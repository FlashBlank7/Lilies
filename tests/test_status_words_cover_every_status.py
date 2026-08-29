"""状态码的中文对照表，必须盖住代码里真会出现的每一个状态。

回归背景：状态码泄漏在这个项目上反复发生过——
真机上业主看到过「4 个需要关注（needs_attention）」，
recent_runs 里的 status 因为嵌在列表推导里绕过了 AST 那道门。
每次修的都是"这一处翻了"，而没人盯着"表里有没有漏项"。

对照表用的都是 `.get(status, status)` 这种写法：**漏一项就原样漏出去**，
而且悄无声息——多一个状态的那天，没有任何东西会红。

这里把权威定义在实现里：runtime / storage / builder 写进库的那些
status="..." 字面量就是全集。表盖不住就红。

（这是"检查表和被检查的东西各写一份，迟早分家"的同一个教训。
  今天在冒烟脚本的内部词清单上刚踩过一次。）
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "platform" / "backend" / "src" / "agent_platform"


def _literals(filename: str, pattern: str) -> set[str]:
    text = (SRC / filename).read_text(encoding="utf-8")
    return set(re.findall(pattern, text))


class RunStatusWordsTest(unittest.TestCase):
    def test_every_run_status_written_to_the_db_has_a_chinese_word(self):
        from agent_platform.assistant_agent import _RUN_WORDS

        written = _literals("workflow_runtime.py", r'status\s*=\s*"([a-z_]+)"')
        # 只关心运行状态：任务/构建那几套走别的表
        interesting = written & {"queued", "running", "succeeded", "failed",
                                 "paused", "cancelled", "expired", "timed_out",
                                 "skipped", "blocked", "aborted"}
        missing = sorted(interesting - set(_RUN_WORDS))
        self.assertEqual(missing, [],
                         f"这些运行状态会原样漏给用户：{missing}")

    def test_the_table_is_not_padded_with_statuses_that_never_happen(self):
        """反向也查一下：表里的每一项都该是真会出现的状态。

        多余项本身无害，但它会让人以为覆盖过了——
        而实际漏的那个恰恰不在表里。
        """
        from agent_platform.assistant_agent import _RUN_WORDS

        written = (_literals("workflow_runtime.py", r'status\s*=\s*"([a-z_]+)"')
                   | _literals("workflow_storage.py", r'status\s*=\s*"([a-z_]+)"')
                   | _literals("workflow_storage.py", r"'([a-z_]+)'")
                   | _literals("workflow_runtime.py", r'"status"\]\s*==\s*"([a-z_]+)"'))
        never = sorted(set(_RUN_WORDS) - written)
        self.assertEqual(never, [], f"表里这些状态代码里根本不会出现：{never}")

    def test_no_word_is_still_an_english_code(self):
        """翻译成英文原样也算没翻——断言要比"有个值"强。"""
        from agent_platform.assistant_agent import _RUN_WORDS

        for code, word in _RUN_WORDS.items():
            self.assertNotEqual(code, word, code)
            self.assertFalse(re.fullmatch(r"[A-Za-z_ ]+", word), f"{code} → {word}")


def _build_statuses_written() -> set[str]:
    """代码里真会写进 builds.status 的值——从 update_build(...) 的调用里取。

    注意别把 harness.finish_task(status=...) 混进来：那是**任务**状态
    （paused / succeeded 只在那儿出现），跟构建状态不是一回事。
    第一版就是这么混的，差点为两个根本不会出现的状态去补文案。
    """
    found: set[str] = set()
    for path in SRC.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for call in re.findall(r"update_build\((?:[^()]|\([^()]*\))*\)", text):
            found.update(re.findall(r'status\s*=\s*"([a-z_]+)"', call))
    return found


class BuildStatusWordsTest(unittest.TestCase):
    UNKNOWN = "搭建状态未知"

    def test_every_real_build_status_gets_its_own_sentence(self):
        """落到兜底句不只是"不好看"——兜底给的建议是「让它接着跑试试」。

        对一个**已发布**或**已放弃**的构建说这句，是把已经做完的决定
        又翻出来。代码里给 cancelled 单独写一支就是为了这个；
        这条测试保证下一个新状态也别落进去。
        """
        from agent_platform.assistant_agent import _build_situation

        statuses = _build_statuses_written() | {"published", "needs_attention",
                                                "cancelled", "failed"}
        self.assertTrue(statuses, "一个构建状态都没扫到，这条测试等于没测")
        fell_through = []
        for status in sorted(statuses):
            situation, _ = _build_situation(status, None, "")
            if situation == self.UNKNOWN:
                fell_through.append(status)
        self.assertEqual(fell_through, [],
                         f"这些构建状态落到了兜底句（会被建议「让它接着跑」）：{fell_through}")

    def test_no_build_status_code_leaks_into_the_words(self):
        from agent_platform.assistant_agent import _build_situation

        for status in sorted(_build_statuses_written()
                             | {"published", "needs_attention", "cancelled",
                                "failed", "这是个没见过的状态"}):
            situation, todo = _build_situation(status, None, "")
            self.assertTrue(situation and todo, status)
            self.assertNotIn(status, situation, f"状态码 {status} 漏出去了")
            self.assertNotIn(status, todo, status)

    def test_an_unheard_of_status_still_gets_the_safe_fallback(self):
        """兜底本身要留着：真冒出没见过的状态，也不能什么都不说。"""
        from agent_platform.assistant_agent import _build_situation

        situation, todo = _build_situation("从未见过的状态", None, "")
        self.assertEqual(situation, self.UNKNOWN)
        self.assertTrue(todo)


if __name__ == "__main__":
    unittest.main()
