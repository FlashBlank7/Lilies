"""模型的格式笔误不该让整场验收抛异常。

回归背景（2026-08-28 真机）：监理把 must_execute / must_not_execute
写在了用例这一层而不是 expect 里。AcceptanceCase 禁止多余字段，于是
    ValidationError: cases.0.must_execute — Extra inputs are not permitted
整个 acceptance_check 抛出去，靠模型自己重试才蒙对。
归一化本来就是为了吸收这类笔误（它的 docstring 就写着「容错归一化模型笔误」）。
"""
import unittest

from agent_platform.acceptance_pm import AcceptanceSpec, normalize_spec_payload

STRAY_KEYS = ("required_fields", "equals", "contains", "not_contains",
              "must_execute", "must_not_execute")


def _spec(case: dict) -> AcceptanceSpec:
    return AcceptanceSpec.model_validate(
        normalize_spec_payload({"summary": "查一查", "cases": [case]}))


class SpecNormalisationTest(unittest.TestCase):
    def test_every_expect_key_written_at_case_level_is_relocated(self):
        for key in STRAY_KEYS:
            value = [] if key in ("required_fields", "must_execute",
                                  "must_not_execute") else {}
            spec = _spec({"name": "c", "inputs": {"a": 1}, key: value})
            self.assertTrue(spec.cases, f"{key} 写在用例层就炸了")

    def test_the_stray_value_is_kept_not_dropped(self):
        spec = _spec({"name": "c", "inputs": {"a": 1},
                      "must_execute": ["llm"]})
        self.assertEqual(spec.cases[0].expect.must_execute, ["llm"])

    def test_a_proper_expect_wins_over_a_stray_duplicate(self):
        spec = _spec({"name": "c", "inputs": {"a": 1},
                      "must_execute": ["写错地方的"],
                      "expect": {"must_execute": ["写对地方的"]}})
        self.assertEqual(spec.cases[0].expect.must_execute, ["写对地方的"])

    def test_normal_shape_is_untouched(self):
        spec = _spec({"name": "c", "inputs": {"a": 1},
                      "expect": {"equals": {"n": 1}}})
        self.assertEqual(spec.cases[0].expect.equals, {"n": 1})
