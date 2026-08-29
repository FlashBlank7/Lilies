"""动作行显示给用户的名字得是中文，不是内部工具名。

回归背景（2026-08-29）：管家每调一个工具，客户端就打一行

    ⚙ recent_runs → 「词频统计」最近 3 次…

`recent_runs` 是内部工具名。回答正文里这类词早就被 _without_tool_names 拦下了，
可动作行是另一条出口，闸没装到那儿——同一个闸没装满所有出口，今天第三次。
（前两次：业主页 kind=event 整类白名单、流式 delta 绕过全部清洗。）

对照表一直都在（_TOOL_WORDS，15 个工具全覆盖），只是没接到这条线上。
"""
import unittest

from agent_platform.assistant_agent import _TOOL_WORDS, TOOLS, _summarize


class ToolLabelTest(unittest.TestCase):
    def test_every_tool_has_a_chinese_name(self):
        names = {t["name"] if isinstance(t, dict) else getattr(t, "name", "")
                 for t in TOOLS}
        missing = sorted(n for n in names if n and n not in _TOOL_WORDS)
        self.assertEqual(missing, [], f"这些工具没有中文名，会原样显示给用户：{missing}")

    def test_no_label_is_just_the_tool_name_again(self):
        """对照表里填成英文原名等于没填。"""
        import re
        latin = [k for k, v in _TOOL_WORDS.items() if re.search(r"[A-Za-z_]{3,}", v)]
        self.assertEqual(latin, [], f"这些中文名里还带着英文：{latin}")

    def test_the_labels_are_distinct(self):
        """全都翻成同一句话的话，用户看不出它在干什么。"""
        self.assertEqual(len(set(_TOOL_WORDS.values())), len(_TOOL_WORDS))


class ActionEventCarriesTheLabelTest(unittest.IsolatedAsyncioTestCase):
    """接线才是关键：对照表在、事件里不带，用户还是看英文。

    这里真跑一遍 reply()，把发出去的事件收下来看——
    初稿写的是 inspect.getsource + 查有没有 '"label"' 这个串，
    那是断言源码长什么样，改个写法就骗过去了。
    """

    async def test_the_action_event_carries_a_chinese_label(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from agent_platform.assistant_agent import WorkflowConcierge

        agent = WorkflowConcierge.__new__(WorkflowConcierge)
        agent.services = SimpleNamespace(
            provider=SimpleNamespace(stream=lambda **k: None),
            storage=SimpleNamespace(append_event=AsyncMock()))
        agent.settings = SimpleNamespace(deepseek_runtime_model="stub")
        agent._exec = AsyncMock(return_value={"runs": []})

        # 用真的 ContentBlock：中间会塞进 ChatMessage，pydantic 会校验
        from agent_platform.models import ContentBlock

        call = ContentBlock(type="tool_use", name="recent_runs", id="c1", input={})
        done = ContentBlock(type="text", text="最近跑了 3 次。")
        responses = [SimpleNamespace(blocks=[call]), SimpleNamespace(blocks=[done])]

        async def fake_collect(stream, **kwargs):
            return responses.pop(0)

        events: list[dict] = []

        async def collect(event):
            events.append(event)

        with patch("agent_platform.assistant_agent.collect_model_stream",
                   side_effect=fake_collect):
            await agent.reply([{"role": "user", "text": "最近跑了几次"}], {}, emit=collect)

        actions = [e for e in events if e.get("type") == "action"]
        self.assertTrue(actions, f"没发出动作事件：{events}")
        self.assertEqual(actions[0]["label"], "查运行记录")
        self.assertEqual(actions[0]["tool"], "recent_runs",
                         "tool 不能去掉——客户端旧版本和事件统计都认它")


if __name__ == "__main__":
    unittest.main()


class ActionSummaryIsForUsersTest(unittest.TestCase):
    """动作行那句摘要也是给用户看的，同样不许出现内部工具名。

    工具的 error 文案是写给模型的（「没给构建号。用 recent_builds 找到那一个」），
    可它原样进 summary，于是界面上写着 recent_builds。
    修在 _summarize 这个边界上——将来新加的工具错误也一样受用。
    """

    def test_a_tool_name_in_an_error_is_translated(self):
        from agent_platform.assistant_agent import _summarize

        summary = _summarize({"error": "没给构建号。用 recent_builds 找到那一个再来。"})
        self.assertNotIn("recent_builds", summary)
        self.assertIn("生成任务", summary)

    def test_every_tool_error_string_in_the_module_is_clean_after_summarising(self):
        """把源码里所有工具 error 文案都过一遍，一个都不许漏。"""
        import re

        from agent_platform.assistant_agent import _summarize
        from pathlib import Path
        import agent_platform.assistant_agent as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        leaked = []
        for match in re.finditer(r'"error":\s*f?"([^"]{4,})"', source):
            text = re.sub(r"\{[^}]*\}", "", match.group(1))
            summary = _summarize({"error": text})
            if re.search(r"[a-z_]{4,}_[a-z_]{3,}", summary):
                leaked.append(summary)
        self.assertEqual(leaked, [], f"动作行会显示内部名字：{leaked}")

    def test_a_normal_summary_is_untouched(self):
        from agent_platform.assistant_agent import _summarize

        self.assertEqual(_summarize({"workflows": [], "total": 3}), "3 个工作流")


class SmokeChecksEveryToolNameTest(unittest.TestCase):
    """冒烟脚本那把"内部词"的尺子，必须量得住**每一个**工具名。

    2026-08-29：加了 run_counts，而脚本里是一份手抄的 15 个名字的清单，
    没跟着改——新工具的名字漏进回答里，冒烟一路绿灯。
    检查表和被检查的东西各写一份，迟早分家；分家之后它还报绿，
    比没有这项检查更让人放心。

    脚本已改成从 TOOLS 里取。这条测试盯着它别再退回手抄。
    """

    def test_every_tool_name_is_caught_by_the_smoke_regex(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import smoke_concierge

        missed = [t.name for t in TOOLS
                  if t.name and not smoke_concierge.INTERNAL_WORDS.search(
                      f"我这就用 {t.name} 查一下")]
        self.assertEqual(missed, [], f"这些工具名漏进回答里冒烟也看不出来：{missed}")

    def test_it_does_not_flag_an_ordinary_answer(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import smoke_concierge

        for ordinary in ("今天跑了 1 次，全部成功。",
                         "词频统计昨天跑了 3 次，没有失败。",
                         "已发布的工作流有 3 个。"):
            self.assertIsNone(smoke_concierge.INTERNAL_WORDS.search(ordinary), ordinary)


class EveryDeclaredToolIsImplementedTest(unittest.IsolatedAsyncioTestCase):
    """声明了的工具都得真能跑，跑得了的都得声明过。

    工具清单（TOOLS）和实现（_exec 里那串 if）是两处分开写的。
    只声明不实现的后果很温和：模型会去调它，每次都拿到
    「没有这个工具」，然后换别的路子——业主看到的是"它就是不会做这件事"，
    而不是"这里有个 bug"。反过来，实现了不声明就等于白写：
    模型根本不知道有这个工具。

    这条不去 grep 源码里的 `if name ==`，而是**真的调一遍**：
    源码断言换个写法（比如改成字典分派）就骗过去了。
    """

    async def _call(self, name: str):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from agent_platform.assistant_agent import WorkflowConcierge

        concierge = WorkflowConcierge(MagicMock(), SimpleNamespace())
        try:
            return await concierge._exec(name, {}, {})
        except Exception:      # noqa: BLE001 - 抛异常也算"能走到"，不算没实现
            return {"reached": True}

    async def test_no_declared_tool_answers_no_such_tool(self):
        missing = []
        for tool in TOOLS:
            result = await self._call(tool.name)
            if isinstance(result, dict) and "没有「" in str(result.get("error", "")):
                missing.append(tool.name)
        self.assertEqual(missing, [], f"声明了却没实现的工具：{missing}")

    async def test_an_undeclared_name_does_say_no_such_tool(self):
        """兜底本身要在：模型填错名字时得到一句能读懂的话，而不是 500。"""
        result = await self._call("根本没有这个工具")
        self.assertIn("没有「", str(result.get("error", "")))
        self.assertIn("如实告诉用户", str(result.get("error", "")))


class TruncationInTheActionLineIsVisible(unittest.TestCase):
    """动作行截了也要说。上面 _capped 早就是这个调子，这两处漏了。

    真机量过：160 次成功运行里 52 次（33%）至少有一项产出超过 30 字。
    每三次就有一次，用户看到的是一句砍在中间的值、且看不出被砍过。
    """

    def test_a_long_output_value_ends_with_an_ellipsis(self):
        line = _summarize({"outputs": {"report": "各门店合计：" + "北京 120，" * 20},
                           "情况": "跑成了"})
        self.assertTrue(line.endswith("…"), line[-40:])

    def test_a_short_output_value_is_left_alone(self):
        """反向：不能给每个值都缀省略号。"""
        line = _summarize({"outputs": {"total": 42}, "情况": "跑成了"})
        self.assertIn("total=42", line)
        self.assertNotIn("…", line)

    def test_a_long_error_ends_with_an_ellipsis(self):
        line = _summarize({"error": "出事了：" + "原因" * 60})
        self.assertTrue(line.endswith("…"), line[-40:])

    def test_a_short_error_is_left_alone(self):
        line = _summarize({"error": "没有叫「日报」的工作流"})
        self.assertNotIn("…", line)

    def test_the_key_name_is_never_the_part_that_gets_cut(self):
        """截的是值不是键——键没了就不知道这是什么。"""
        line = _summarize({"outputs": {"门店合计报表": "补" * 200}, "情况": "跑成了"})
        self.assertIn("门店合计报表=", line)
