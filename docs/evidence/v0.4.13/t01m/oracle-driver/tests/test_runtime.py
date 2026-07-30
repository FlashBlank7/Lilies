import json
import tempfile
import unittest
from pathlib import Path

from t01m_host.constants import APPLICATION_ID
from t01m_host.runtime import (
    analyze_accessibility_hierarchy,
    compare_runtime_states,
    verify_focus_sequence,
    verify_focus_signature_cycle,
)
from t01m_host.util import OracleError, canonical_json_bytes


class RuntimeTests(unittest.TestCase):
    def test_accessibility_touch_target_and_chinese_name(self):
        report = analyze_accessibility_hierarchy(
            {
                "nodes": [
                    {
                        "path": "0.1",
                        "bounds": "[0,0][96,96]",
                        "enabled": True,
                        "clickable": True,
                        "visible_to_user": True,
                        "text": "",
                        "content_description": "删除",
                    }
                ]
            },
            density=2.0,
        )
        self.assertEqual(report["result"], "pass")

    def test_numeric_choice_uses_chinese_content_description(self):
        report = analyze_accessibility_hierarchy(
            {
                "nodes": [
                    {
                        "path": "0.1",
                        "bounds": "[0,0][96,96]",
                        "enabled": True,
                        "clickable": True,
                        "text": "1",
                        "content_description": "优先级 1",
                    }
                ]
            },
            density=2.0,
        )
        self.assertEqual(report["result"], "pass")

    def test_focus_sequence_is_exact(self):
        trace = {
            "focus_events": [
                {"text": "文明火种", "content_description": "", "class": "Heading"}
            ]
        }
        report = verify_focus_sequence(
            trace, [{"name": "文明火种", "class": "Heading"}]
        )
        self.assertEqual(report["event_count"], 1)

    def test_real_talkback_cycle_requires_exact_return_to_first(self):
        expected = ["火种库|heading", "筛选|button"]
        trace = {
            "focus_events": [
                {
                    "package": APPLICATION_ID,
                    "text": "火种库",
                    "content_description": "",
                    "class": "android.widget.TextView",
                    "heading": True,
                    "clickable": False,
                },
                {
                    "package": APPLICATION_ID,
                    "text": "筛选",
                    "content_description": "",
                    "class": "android.widget.Button",
                    "heading": False,
                    "clickable": True,
                },
                {
                    "package": APPLICATION_ID,
                    "text": "火种库",
                    "content_description": "",
                    "class": "android.widget.TextView",
                    "heading": True,
                    "clickable": False,
                },
            ],
            "dumpsys_accessibility_after_each_gesture": [
                {
                    "gesture_index": index,
                    "talkback_present": True,
                    "touch_exploration_enabled": True,
                    "dumpsys_utf8": "TalkBack touchExplorationEnabled=true",
                }
                for index in range(3)
            ],
        }
        self.assertEqual(
            verify_focus_signature_cycle(trace, expected)["event_count"], 3
        )
        trace["focus_events"].pop()
        with self.assertRaises(OracleError):
            verify_focus_signature_cycle(trace, expected)

    def test_runtime_comparison_rejects_a_new_uid_socket(self):
        tables = {"tcp", "tcp6", "udp", "udp6"}

        def state():
            return {
                "application_id": APPLICATION_ID,
                "uid": "10123",
                "phase": "prelaunch",
                "logcat_cursor_started_unix_ns": 10,
                "airplane_mode_on": "1",
                "wifi_state": "Wi-Fi is disabled",
                "mobile_data_setting": "0",
                "crash_anr_markers": [],
                "uid_netstats_lines": [],
                "uid_socket_rows": {name: [] for name in tables},
                "proc_net": {
                    name: {"readable": True, "sha256": "a" * 64, "content": ""}
                    for name in tables
                },
                "socket_attempt_markers": [],
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = state()
            after = state()
            after["uid_socket_rows"]["tcp"] = ["fixture socket row"]
            (root / "before.json").write_bytes(canonical_json_bytes(before))
            (root / "after.json").write_bytes(canonical_json_bytes(after))
            with self.assertRaises(OracleError):
                compare_runtime_states(root / "before.json", root / "after.json")

    def test_runtime_comparison_rejects_a_transient_interval_socket(self):
        tables = {"tcp", "tcp6", "udp", "udp6"}
        before = {
            "application_id": APPLICATION_ID,
            "uid": "10123",
            "phase": "prelaunch",
            "logcat_cursor_started_unix_ns": 10,
            "airplane_mode_on": "1",
            "wifi_state": "Wi-Fi is disabled",
            "mobile_data_setting": "0",
            "crash_anr_markers": [],
            "uid_netstats_lines": [],
            "uid_socket_rows": {name: [] for name in tables},
            "proc_net": {
                name: {"readable": True, "sha256": "a" * 64, "content": ""}
                for name in tables
            },
            "socket_attempt_markers": [],
        }
        after = json.loads(json.dumps(before))
        after["phase"] = "post_workload"
        interval = {
            "schema_version": 1,
            "application_id": APPLICATION_ID,
            "uid": "10123",
            "started_at_unix_ns": 11,
            "completed_at_unix_ns": 20,
            "sample_interval_milliseconds": 100,
            "sample_count": 2,
            "nonempty_observations": [
                {
                    "sampled_at_unix_ns": 15,
                    "uid_socket_rows": {
                        "tcp": ["transient socket row"],
                        "tcp6": [],
                        "udp": [],
                        "udp6": [],
                    },
                }
            ],
            "all_tables_readable": True,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "before.json").write_bytes(canonical_json_bytes(before))
            (root / "after.json").write_bytes(canonical_json_bytes(after))
            (root / "interval.json").write_bytes(canonical_json_bytes(interval))
            with self.assertRaises(OracleError):
                compare_runtime_states(
                    root / "before.json",
                    root / "after.json",
                    socket_interval_path=root / "interval.json",
                )


if __name__ == "__main__":
    unittest.main()
