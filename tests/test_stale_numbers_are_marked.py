"""上一轮说过的数字，下一轮不能当依据。

真机实测（2026-08-29）：上文里放一句「一共 40 个工作流，25 个已发布」，
再问「那已发布的占比是多少」——它**一次工具都没调**，直接答 62.5%。
真值是 15 个里 3 个 = 20%。
同一轮的另外两个探测（编一个不存在的工作流、谎称做过一个动作）
它都去查了并当场戳穿；差别在于这一问长得像纯算术题：
数字"已经在手上"，看不出还需要查。

**这是缓解，不是根治。**实测数据（每种条件各 4–6 次）：

    不加提醒        3/8  去查了
    温和措辞        4/8
    加重措辞        3/6（占比这一问，从 0/4 提到 3/6）

而且有一次它查了、却仍旧用旧数作答。提示词层面的东西就是这样——
今天已经反复印证「禁令是请求不是保证」。留着它是因为有量到的收益、
运行时又不花钱；不留是因为它解决不了问题。别把它当成解决了。

能机械保证的那部分是这里测的：标记贴没贴对地方、会不会误伤、
会不会漏到用户眼前。
"""
import unittest

from agent_platform.assistant_agent import (WorkflowConcierge,
                                            _without_context_marks)

history = WorkflowConcierge._history_text


class StaleNumberMarkTest(unittest.TestCase):
    def test_an_assistant_turn_with_counts_gets_the_reminder(self):
        marked = history({"role": "assistant",
                          "text": "一共有 40 个工作流，其中 25 个已发布。"})
        self.assertIn("旧数", marked)
        self.assertIn("重新查", marked)

    def test_the_original_text_survives(self):
        """提醒是加上去的，不是替换——原话丢了下一轮就没上下文了。"""
        marked = history({"role": "assistant", "text": "昨天失败 5 次。"})
        self.assertIn("昨天失败 5 次。", marked)

    def test_a_turn_without_numbers_is_left_alone(self):
        """每条都贴就成了噪音，噪音会让它整体不当回事。"""
        for plain in ("好的，我这就去看看。", "已经帮你收起来了。",
                      "你说的是哪一个工作流？"):
            self.assertEqual(history({"role": "assistant", "text": plain}), plain)

    def test_a_version_number_is_not_a_statistic(self):
        """「版本 1」不是会变的统计量，而它几乎每条回答都带。"""
        plain = "已发布的是「词频统计」（版本 1）。"
        self.assertEqual(history({"role": "assistant", "text": plain}), plain)

    def test_the_user_turn_is_never_marked(self):
        """业主自己说的话一个字都不能动——那是核对指示的权威来源。"""
        said = "昨天失败 5 次吗？"
        self.assertEqual(history({"role": "user", "text": said}), said)

    def test_it_coexists_with_the_action_mark(self):
        marked = history({"role": "assistant", "text": "一共 40 个。",
                          "actions": [{"tool": "list_workflows"}]})
        self.assertIn("上一轮做了", marked)
        self.assertIn("旧数", marked)

    def test_the_mark_never_reaches_the_user(self):
        """标记是给模型看的。漏到回答里就是又一次"内部记号泄漏"。"""
        for text in ("一共 40 个。", "昨天失败 5 次。"):
            marked = history({"role": "assistant", "text": text,
                              "actions": [{"tool": "list_workflows"}]})
            cleaned = _without_context_marks(marked)
            self.assertNotIn("上下文", cleaned)
            self.assertNotIn("旧数", cleaned)
            self.assertIn(text.rstrip("。"), cleaned)


class BlankAnswerTest(unittest.IsolatedAsyncioTestCase):
    """一句「（无回复）」是死胡同：用户不知道是自己问得不对、
    是平台坏了、还是该重说一遍。流式那条路早就有能行动的话了，这条没有。

    这条第一版写成了"扫 reply 的源码里不许出现那四个字"——结果被我
    **自己写的注释**绊倒（注释里引用了旧文案）。源码断言就是这样：
    它量的是字符出现，不是行为。改成真跑一次。
    """

    async def _reply_with(self, blocks):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from agent_platform.assistant_agent import WorkflowConcierge

        concierge = WorkflowConcierge(
            SimpleNamespace(provider=SimpleNamespace(stream=lambda **k: None),
                            storage=SimpleNamespace(append_event=AsyncMock())),
            SimpleNamespace(deepseek_runtime_model="m"))
        response = SimpleNamespace(blocks=blocks)
        with patch("agent_platform.assistant_agent.collect_model_stream",
                   AsyncMock(return_value=response)):
            _, text = await concierge.reply([{"role": "user", "text": "在吗"}], {})
        return text

    async def test_a_blank_model_turn_tells_the_user_what_to_do(self):
        text = await self._reply_with([])
        self.assertNotIn("（无回复）", text)
        self.assertIn("再问一次", text)
        self.assertIn("日志", text)      # 一直这样时的下一步

    async def test_a_normal_answer_is_untouched(self):
        from types import SimpleNamespace

        text = await self._reply_with(
            [SimpleNamespace(type="text", text="今天跑了 1 次。")])
        self.assertEqual(text, "今天跑了 1 次。")


if __name__ == "__main__":
    unittest.main()
