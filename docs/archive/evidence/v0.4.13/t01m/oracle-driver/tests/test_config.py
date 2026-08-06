import unittest

from t01m_host.config import load_accessibility_contract, load_flow


class ConfigTests(unittest.TestCase):
    def test_frozen_a06_compiles_to_semantic_atomic_steps(self):
        flow = load_flow()
        self.assertEqual(len(flow["steps"]), 473)
        self.assertEqual(
            [step["number"] for step in flow["steps"]],
            list(range(1, len(flow["steps"]) + 1)),
        )
        serialized = repr(flow["steps"])
        for forbidden in ("tap_x", "tap_y", "global_ordinal", "coordinates"):
            self.assertNotIn(forbidden, serialized)

    def test_unicode_boundaries_are_code_point_exact(self):
        flow = load_flow()
        fixtures = flow["unicode_boundaries"]
        self.assertEqual(len(fixtures["supplementary_40"]), 40)
        self.assertEqual(len(fixtures["supplementary_41"]), 41)
        self.assertEqual(len(fixtures["decomposed_40"]), 80)
        self.assertEqual(len(fixtures["decomposed_41"]), 82)
        self.assertEqual(len(fixtures["notes_supplementary_300"]), 300)
        self.assertEqual(len(fixtures["notes_supplementary_301"]), 301)
        self.assertEqual(len(fixtures["notes_decomposed_300"]), 600)
        self.assertEqual(len(fixtures["notes_decomposed_301"]), 602)
        white_space = "".join(
            chr(value)
            for value in (
                *range(0x0009, 0x000E),
                0x0020,
                0x0085,
                0x00A0,
                0x1680,
                *range(0x2000, 0x200B),
                0x2028,
                0x2029,
                0x202F,
                0x205F,
                0x3000,
            )
        )
        self.assertEqual(len(white_space), 25)
        self.assertEqual(fixtures["white_space_15"], white_space)
        self.assertEqual(fixtures["white_space_15_reverse"], white_space[::-1])
        self.assertEqual(fixtures["trim_name"], white_space + "已修剪" + white_space[::-1])
        self.assertEqual(fixtures["internal_white_space_name"], "内" + white_space + "部")
        self.assertEqual(fixtures["non_trim_members_name"], "\u200b不应修剪\ufeff")
        self.assertEqual(fixtures["notes_white_space"], white_space + "保留" + white_space[::-1])
        self.assertEqual(
            flow["utf16_hex_boundaries"],
            ["d800", "dc00", "0041d8000042", "0041dc000042"],
        )
        self.assertEqual(
            [
                step["value_utf16_hex"]
                for step in flow["steps"]
                if step["action"] == "set_text_utf16_hex"
            ],
            flow["utf16_hex_boundaries"] + flow["utf16_hex_boundaries"],
        )

    def test_a08_is_exactly_ten_screens_at_both_font_scales(self):
        contract = load_accessibility_contract()
        self.assertEqual(contract["font_scales"], [1.0, 2.0])
        self.assertEqual(len(contract["screens"]), 10)
        self.assertEqual(
            len(contract["font_scales"]) * len(contract["screens"]), 20
        )

    def test_every_mutation_retains_evidence(self):
        flow = load_flow()
        mutations = {
            "back",
            "click",
            "force_stop_relaunch",
            "rotate",
            "set_text",
            "talkback_next",
        }
        self.assertTrue(any(step["action"] in mutations for step in flow["steps"]))
        self.assertFalse(
            any(
                step["action"] in mutations and step.get("evidence") is False
                for step in flow["steps"]
            )
        )


if __name__ == "__main__":
    unittest.main()
