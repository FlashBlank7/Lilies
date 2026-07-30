import json
import tempfile
import unittest
from pathlib import Path

from t01m_host.driver import SemanticDriver
from t01m_host.util import OracleError
from t01m_host.workflow import evaluate_assertion


class _Result:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    def text(self):
        return self.stdout.decode()


class _Device:
    serial = "fixture"

    def __init__(self):
        self.arguments = None

    def shell(self, *args, **kwargs):
        self.arguments = args
        return _Result(
            stdout=(
                b"INSTRUMENTATION_RESULT: status=pass\n"
                b"INSTRUMENTATION_CODE: -1\n"
            )
        )


class DriverTests(unittest.TestCase):
    def test_unicode_and_scope_are_base64_transport(self):
        device = _Device()
        driver = SemanticDriver(device, Path("/unused"))
        driver.invoke(
            "set_text",
            selector="火种名称",
            scope="火种：Å；类别：生存基础；优先级：3；状态：沉睡",
            value="🌱e\u0301",
        )
        args = list(device.arguments)
        self.assertIn("selector_base64", args)
        self.assertIn("scope_selector_base64", args)
        self.assertIn("value_base64", args)
        self.assertNotIn("火种名称", args)
        self.assertNotIn("🌱e\u0301", args)

    def test_invalid_utf16_uses_ascii_hex_not_utf8_or_base64(self):
        device = _Device()
        driver = SemanticDriver(device, Path("/unused"))
        driver.invoke(
            "set_text_utf16_hex",
            selector="火种名称",
            utf16_hex="0041d8000042",
        )
        args = list(device.arguments)
        self.assertIn("value_utf16_hex", args)
        self.assertIn("0041d8000042", args)
        self.assertNotIn("value_base64", args)

    def test_scoped_assertion_never_uses_global_ordinal(self):
        hierarchy = {
            "nodes": [
                {
                    "path": "0.1",
                    "text": "",
                    "content_description": "火种：一；类别：生存基础；优先级：3；状态：沉睡",
                },
                {"path": "0.1.1", "text": "删除", "content_description": ""},
                {
                    "path": "0.2",
                    "text": "",
                    "content_description": "火种：二；类别：生存基础；优先级：3；状态：沉睡",
                },
                {"path": "0.2.1", "text": "删除", "content_description": ""},
            ]
        }
        result = evaluate_assertion(
            hierarchy,
            {
                "action": "assert_visible",
                "scope": "火种：二；类别：生存基础；优先级：3；状态：沉睡",
                "assertion": {"text": "删除"},
            },
        )
        self.assertEqual(result["observed_count"], 1)

    def test_offline_placeholder_binds_exactly_once(self):
        hierarchy = {
            "nodes": [
                {
                    "path": "0.1",
                    "text": "完全离线运行，数据只保存在本机，不会上传。",
                    "content_description": "",
                }
            ]
        }
        observed = evaluate_assertion(
            hierarchy,
            {
                "action": "assert_offline_private",
                "assertion": {"binding": "fixture"},
            },
        )
        self.assertEqual(
            observed["qualifying_texts"],
            ["完全离线运行，数据只保存在本机，不会上传。"],
        )

    def test_exact_choice_order_rejects_an_extra_value(self):
        hierarchy = {
            "nodes": [
                {
                    "path": f"0.{index}",
                    "text": value,
                    "content_description": "",
                    "class": "android.widget.RadioButton",
                    "checkable": True,
                }
                for index, value in enumerate(
                    ["生存基础", "知识传承", "额外类别", "能源设施", "医疗护理", "通信网络"]
                )
            ]
        }
        with self.assertRaises(OracleError):
            evaluate_assertion(
                hierarchy,
                {
                    "action": "assert_order",
                    "assertion": {
                        "texts": ["生存基础", "知识传承", "能源设施", "医疗护理", "通信网络"],
                        "exact": True,
                        "domain_role": "choice",
                    },
                },
            )

    def test_record_set_rejects_extra_or_reordered_cards(self):
        expected = [
            "火种：甲；类别：生存基础；优先级：5；状态：沉睡",
            "火种：乙；类别：能源设施；优先级：3；状态：沉睡",
        ]
        hierarchy = {
            "nodes": [
                {"path": "0.1", "text": value, "content_description": ""}
                for value in [expected[1], expected[0], "火种：额外；类别：医疗护理；优先级：1；状态：沉睡"]
            ]
        }
        with self.assertRaises(OracleError):
            evaluate_assertion(
                hierarchy,
                {
                    "action": "assert_record_set",
                    "assertion": {"texts": expected},
                },
            )


if __name__ == "__main__":
    unittest.main()
