"""业主能看到哪些里程碑，得由写入点说了算，不能整类白名单。

回归背景（2026-08-29 真机测量）：闸口把 kind=event 整类放行，
理由是"里程碑都是给业主看的"。可 event 里 368 条是 event="phase"——
构建状态机的内部诊断。其中 **85 条**带着模型名或英文报错：

    分工：提案者=local/Qwen/Qwen3-4B-Instruct-2507；架构选型=local2/Qwen/Qwen3-32B
    架构规划：local2/Qwen/Qwen3-32B 选型
    第 3 轮修复升级到更强模型：local2/Qwen/Qwen3-32B
    小模型修复卡住（model perseverating: identical rejected proposal 3x），提前升级到 …

这些直接摆在业主面前。闸口挡住了 turn 正文，却在 event 这一侧开了个整类的口子。

改法不是去扫文本（扫得再干净，下一个写入点照样漏），
而是**默认不给**：写入点显式 for_owner=True 才放行。
方向是故意选的——将来忘了标注，业主少看一行内部日志，
而不是多看一行模型名。
"""
import unittest

from agent_platform.api import _owner_safe_records
from agent_platform.build_transcript import event_record


def _turn(text: str) -> dict:
    return {"kind": "turn", "text": text, "actor": "builder"}


class OwnerEventsAreOptInTest(unittest.TestCase):
    def _texts(self, records):
        return [str(r.get("text") or "") for r in _owner_safe_records(records)
                if str(r.get("text") or "").strip()]

    def test_phase_diagnostics_do_not_reach_the_owner(self):
        leaked = self._texts([
            event_record(event="phase", text="架构规划：local2/Qwen/Qwen3-32B 选型"),
            event_record(event="phase", text="分工：提案者=local/Qwen/Qwen3-4B-Instruct-2507"),
            event_record(event="phase", text="第 3 轮修复升级到更强模型：local2/Qwen/Qwen3-32B"),
            event_record(event="phase",
                         text="小模型修复卡住（model perseverating: identical rejected "
                              "proposal 3x），提前升级到 local2/Qwen/Qwen3-32B"),
        ])
        self.assertEqual(leaked, [], f"内部诊断漏到业主页：{leaked}")

    def test_an_explicitly_marked_phase_does_reach_the_owner(self):
        """默认不给，不等于给不了——业主等的那句要能过去。"""
        texts = self._texts([
            event_record(event="phase", text="验收测试全绿，交付成立", for_owner=True)])
        self.assertEqual(texts, ["验收测试全绿，交付成立"])

    def test_publish_and_attention_still_reach_the_owner(self):
        """老记录没有 for_owner 这个键，但「已发布」当时就该给业主看。"""
        texts = self._texts([
            {"kind": "event", "event": "published",
             "text": "工作流已发布为正式版 v1，现在可以试运行了"},
            {"kind": "event", "event": "needs_attention",
             "text": "搭建中途遇到问题停下来了"},
        ])
        self.assertEqual(len(texts), 2, "老记录里的真里程碑被误挡了")

    def test_an_unmarked_legacy_phase_is_withheld(self):
        """老 phase 记录同样按默认拒绝处理——它们正是泄漏的那 85 条。"""
        self.assertEqual(self._texts([
            {"kind": "event", "event": "phase",
             "text": "架构规划：local2/Qwen/Qwen3-32B 选型"}]), [])

    def test_self_healing_retries_are_not_owner_business(self):
        """truncated：这一轮输出超长，自己重试一次就过去了。

        业主看了做不了任何事，却平白读到「思考超出输出上限」
        「已提醒构建方压缩思考」这类内部机制。真机 17 条。
        判据是「他看了能做什么」，不是「这算不算里程碑」。
        """
        self.assertEqual(self._texts([
            {"kind": "event", "event": "truncated",
             "text": "这一轮思考超出输出上限被截断；已提醒构建方压缩思考、直接行动"}]), [])

    def test_owner_own_words_are_never_withheld(self):
        """业主自己写的需求原文照原样回显，哪怕里面有英文。"""
        own = "输入姓名 name（字符串），输出 greeting = Hello Ada"
        self.assertEqual(self._texts([{"kind": "owner", "text": own}]), [own])

    def test_turn_prose_is_still_withheld(self):
        self.assertEqual(self._texts([_turn("我删除了 aggregator→assigner 的边")]), [])

    def test_default_is_deny_not_allow(self):
        """这条是方向本身：不写 for_owner 就是不给。

        写入点有 28 个，将来还会加。默认放行的话，
        每加一个都得记得挡一次；默认拒绝，忘了只是少显示。
        """
        self.assertFalse(event_record(event="phase", text="随便什么")["for_owner"])


# 这里原本还有一条 grep api.py 源码、确认闸有调用点的断言。删了——
# 真正的端点级验证已经在 tests/test_owner_transcript_filter.py 里
# （test_builder_prose_never_leaves_the_endpoint，走真 TestClient）。
# 再加一条断言源码长什么样的，只是多一处会因改写法而误红的地方。


if __name__ == "__main__":
    unittest.main()
