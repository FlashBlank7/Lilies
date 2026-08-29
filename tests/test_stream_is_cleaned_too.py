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


class ApologisingForSomethingNobodySaidTest(unittest.TestCase):
    """业主没说过话，别向他道歉。

    「空手报数字就打回重查」带出来的：回炉提示是平台自动加的，
    模型把它当成业主在挑错，于是回一句

        您说得对，我上一轮没查工具就直接报了数，抱歉。我现在重新查一遍。…

    而业主根本没说过话——他看到的是一段没头没尾的道歉。
    （真机 REPL 上原样出现过。）

    提示那一侧已经写清"这不是用户说的话、别道歉"，但那是请求不是保证；
    出口这里再兜一道。
    """

    def test_the_apology_preamble_is_dropped(self):
        got = _without_thinking_aloud(
            "您说得对，我上一轮没查工具就直接报了数，抱歉。"
            "我现在重新查一遍。三个里最不稳的是甲。")
        self.assertEqual(got, "三个里最不稳的是甲。")

    def test_the_short_form_is_dropped_too(self):
        for said in ("我上一轮没查就报了数。今天跑了 1 次。",
                     "抱歉，上一轮没查。今天跑了 1 次。",
                     "我现在重新查一遍。今天跑了 1 次。"):
            self.assertEqual(_without_thinking_aloud(said), "今天跑了 1 次。", said)

    def test_agreeing_with_the_owner_about_something_real_survives(self):
        """「您说得对」本身不是错——业主真说了什么、它同意，那是正常对话。

        见了「您说得对」就删的话，一整类正常回答会被砍头。
        """
        said = "您说得对，这个工作流确实需要修一下。"
        self.assertEqual(_without_thinking_aloud(said), said)

    def test_a_plain_answer_is_untouched(self):
        said = "今天跑了 1 次，全部成功。"
        self.assertEqual(_without_thinking_aloud(said), said)


class FutureTenseNarrationTest(unittest.TestCase):
    """「我查一下…」是预告动作，「我查到…」是在报结果——只删前者。

    真机 REPL 上「我查一下最新数据再答你。」原样出现过。
    这一族此前只覆盖了「我来查/我去查/我这就查」，
    最平常的那个说法反而漏着——挑着列就是给自己留缝，今天第 N 次。
    """

    def test_future_tense_narration_is_dropped(self):
        for said in ("我查一下最新数据再答你。三个里最不稳的是甲。",
                     "我看一遍记录。今天 1 次。",
                     "我查查。今天 1 次。"):
            got = _without_thinking_aloud(said)
            self.assertNotIn("我查", got, said)
            self.assertNotIn("我看", got, said)

    def test_reporting_what_was_found_survives(self):
        """报结果的那些一个字都不能动——删了就把答案本身删了。"""
        for said in ("我查到了 3 个已发布工作流。",
                     "我查了最近 7 天，一共 83 次。",
                     "这个数我算过，是 81.4%。"):
            self.assertEqual(_without_thinking_aloud(said), said, said)

