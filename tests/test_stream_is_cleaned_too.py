"""流式那条路也得清洗——用户看的就是它。

回归背景（2026-08-29）：所有回复清洗（上下文标记、工具名、自言自语）
**只作用于 final 事件**，delta 是原样转发的。而客户端把 delta 逐字打印，
final 只在"一个字都没流过"时才用（cli.py: `if final and not streamed`）。
也就是说这些闸对真正的交互式 CLI 完全没生效，而那正是招牌功能。

实测同一次回答：流式 1283 字、final 1221 字，差的 62 个字用户全看到了。

修法是攒到句子边界再清洗再发：打出去的字收不回来，所以只发完整的、
清洗过的句子。代价是逐句而不是逐字出现。
"""
import unittest

from agent_platform.assistant_agent import (_without_context_marks,
                                            _without_thinking_aloud,
                                            clean_stream)


class ThinkingAloudTest(unittest.TestCase):
    def test_a_narration_sentence_is_dropped(self):
        self.assertEqual(
            _without_thinking_aloud("我需要把这三个都查一遍。今天跑了 35 次。"),
            "今天跑了 35 次。")

    def test_content_before_the_comma_survives(self):
        """一句话里常常前半是内容、后半才是旁白，不能一起删。"""
        self.assertEqual(
            _without_thinking_aloud("有三个已发布工作流，我逐个查它们昨天的记录。跑了 3 次。"),
            "有三个已发布工作流，跑了 3 次。")

    def test_the_whole_let_me_family_is_covered(self):
        """只列「让我看看」的话，「让我查一下」照样出去。

        2026-08-29 在 REPL 上撞到的：招牌路径第一句就是
        「让我查一下平台今天的整体情况。」——模式里只有「让我看看」。
        一族说法要整族覆盖，挑着列就是给自己留缝。
        """
        for narration in ("让我查一下平台情况。", "让我看看。", "让我读一下配置。",
                          "让我试一下。", "让我捋一下。", "让我确认一下分布。",
                          "让我核对一下。", "让我算一下。", "让我数数。",
                          "让我梳理一下。"):
            self.assertEqual(
                _without_thinking_aloud(narration + "今天跑了 3 次。"),
                "今天跑了 3 次。", narration)

    def test_let_me_is_not_over_matched(self):
        """「这个结果让我意外」不是自言自语——别见「让我」就删。"""
        for text in ("这个结果让我意外，但数字是对的。",
                     "这让我想起上次那个问题。",
                     "这个数字让我放心了。"):
            self.assertEqual(_without_thinking_aloud(text), text, text)

    def test_a_filler_is_removed_but_its_sentence_is_kept(self):
        """「实际上，」后面是真信息，整句删掉就把内容弄丢了。"""
        self.assertEqual(
            _without_thinking_aloud("实际上，第三个工作流的记录断了。"),
            "第三个工作流的记录断了。")

    def test_advice_to_the_user_is_untouched(self):
        """「**你**需要把 text 填上」是给用户的建议，不是自言自语。"""
        text = "你需要把 text 这个必填项填上。"
        self.assertEqual(_without_thinking_aloud(text), text)

    def test_a_clean_answer_is_unchanged(self):
        text = "词频统计昨天跑了 3 次，全部成功。"
        self.assertEqual(_without_thinking_aloud(text), text)

    def test_it_runs_as_part_of_the_normal_cleaning_chain(self):
        """挂没挂上链子才是关键——函数写好了没接，等于没写。"""
        self.assertNotIn("我需要把", _without_context_marks("我需要把这个查一下。好了。"))


class CleanStreamTest(unittest.TestCase):
    def test_nothing_is_emitted_before_a_sentence_ends(self):
        out, pending = clean_stream("", "词频统计昨天")
        self.assertEqual(out, "")
        self.assertEqual(pending, "词频统计昨天")

    def test_a_complete_sentence_is_emitted_cleaned(self):
        out, pending = clean_stream("我需要把这个查", "一下。词频")
        self.assertEqual(out, "")          # 整句是旁白，清洗后为空
        self.assertEqual(pending, "词频")

    def test_real_content_gets_through(self):
        out, pending = clean_stream("", "词频统计跑了 3 次。还有")
        self.assertEqual(out, "词频统计跑了 3 次。")
        self.assertEqual(pending, "还有")

    def test_a_newline_also_counts_as_a_boundary(self):
        """列表式回答里常常一行一条，没有句号。"""
        out, _ = clean_stream("", "1. 词频统计 3 次\n2. 日报")
        self.assertIn("词频统计", out)

    def test_the_last_sentence_wins_when_several_arrive_at_once(self):
        out, pending = clean_stream("", "甲。乙。丙")
        self.assertEqual(out, "甲。乙。")
        self.assertEqual(pending, "丙")

    def test_a_tool_name_never_streams(self):
        out, _ = clean_stream("", "我这就用 recent_runs 查一下。")
        self.assertNotIn("recent_runs", out)

    def test_streamed_and_final_say_the_same_thing(self):
        """性质断言：把整段按任意分片喂进去，拼起来要等于整段清洗的结果。

        分片位置是模型吐字的节奏决定的，产品不能因为断在哪儿而说不同的话。
        """
        whole = "我需要把这三个都查一遍。词频统计跑了 3 次。实际上，日报断了。"
        expected = _without_context_marks(whole)
        for size in (1, 3, 7, 40):
            pending, pieces = "", []
            for index in range(0, len(whole), size):
                out, pending = clean_stream(pending, whole[index:index + size])
                pieces.append(out)
            if pending.strip():
                pieces.append(_without_context_marks(pending))
            got = "".join(pieces)
            self.assertEqual(got.replace(" ", ""), expected.replace(" ", ""),
                             f"分片大小 {size} 时说的话不一样")


if __name__ == "__main__":
    unittest.main()
