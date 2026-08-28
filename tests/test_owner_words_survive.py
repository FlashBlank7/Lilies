"""业主的原话要跟着卷子走，判不合格时摆出来给人对照。

回归背景（2026-08-28 真机）：监理把业主的例子换了输入却沿用期望值，
判一个正确的工作流不合格。业主打开验收单只看到「期望 5、实际 11」，
无从发现考题已经跟自己说的不是一回事——出完卷原话就丢了。
"""
import unittest

from agent_platform.acceptance_pm import (AcceptanceSpec, normalize_spec_payload,
                                          render_report_markdown)

OWNER = "输入「甲\n乙\n丙」应该得到行数 3、净字数 5"


def _spec_payload(**extra):
    payload = {
        "summary": "查行数与净字数",
        "cases": [{"name": "三行", "inputs": {"text": "甲\n乙\n丙"},
                   "expect": {"equals": {"line_count": 3}}}],
    }
    payload.update(extra)
    return payload


def _report(*, passed: bool, owner_examples: str):
    return {
        "application_id": "a1", "application_name": "统计", "version": 1,
        "stamp": "2026-08-28 00:00", "summary": "查行数与净字数",
        "required_node_types": [], "required_any_node_types": [],
        "architecture_missing": [], "architecture_pass": True,
        "lineage_missing": [], "lineage_pass": True,
        "cases": [{"name": "三行", "run_status": "succeeded", "passed": passed,
                   "checks": [{"check": "净字数 = 5", "passed": passed, "actual": "11"}]}],
        "passed_cases": 1 if passed else 0, "total_cases": 1,
        "owner_examples": owner_examples, "accepted": passed,
    }


class OwnerWordsSurviveTest(unittest.TestCase):
    def test_spec_keeps_the_owner_words(self):
        spec = AcceptanceSpec.model_validate(
            normalize_spec_payload(_spec_payload(owner_examples=OWNER)))
        self.assertEqual(spec.owner_examples, OWNER)

    def test_paraphrase_and_verbatim_are_kept_apart(self):
        """管家会改写业主的例子——两份分开存，核对才有意义。"""
        spec = AcceptanceSpec.model_validate(normalize_spec_payload(_spec_payload(
            owner_examples="「abc」「de」「f」，行数 3、净字数 5",   # 管家转述的
            owner_words="「第一行」「第二行」「第三行」，行数 3、净字数 5")))  # 业主说的
        self.assertIn("abc", spec.owner_examples)
        self.assertIn("第一行", spec.owner_words)
        self.assertNotIn("abc", spec.owner_words)

    def test_old_spec_without_the_field_still_loads(self):
        # 这个字段是后加的，既有的卷子文件里没有
        spec = AcceptanceSpec.model_validate(normalize_spec_payload(_spec_payload()))
        self.assertEqual(spec.owner_examples, "")

    def test_failing_report_shows_the_owner_words(self):
        text = render_report_markdown(_report(passed=False, owner_examples=OWNER))
        self.assertIn("你当初是这么说的", text)
        self.assertIn("甲", text)
        # 光摆出来还不够，得告诉人这意味着什么
        self.assertIn("卷子出错", text)

    def test_passing_report_stays_quiet(self):
        # 全过的时候没人需要对照，摆着只是噪音
        text = render_report_markdown(_report(passed=True, owner_examples=""))
        self.assertNotIn("你当初是这么说的", text)

    def test_owner_words_with_newlines_stay_inside_the_quote(self):
        text = render_report_markdown(
            _report(passed=False, owner_examples="第一句\n第二句"))
        self.assertIn("> 第一句\n> 第二句", text)
