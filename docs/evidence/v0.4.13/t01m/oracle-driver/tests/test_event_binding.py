import unittest

from t01m_host.event_binding import (
    hierarchy_matches,
    load_reduced_motion_contract,
    validate_idle_capture,
    validate_transition_capture,
)


def _node(path, name, class_name, *, heading=False):
    return {
        "path": path,
        "window_id": 7,
        "class": class_name,
        "text": name,
        "content_description": "",
        "checked": False,
        "selected": False,
        "enabled": True,
        "bounds": "[0,0][100,100]",
        "heading": heading,
        "checkable": False,
        "clickable": class_name.endswith("Button"),
    }


class EventBindingTests(unittest.TestCase):
    def test_hierarchy_match_counts_repeated_signatures_exactly(self):
        hierarchy = {
            "nodes": [
                _node(f"0.{index}", "编辑", "android.widget.Button")
                for index in range(3)
            ]
        }
        self.assertTrue(
            hierarchy_matches(
                hierarchy,
                ["编辑|button", "编辑|button", "编辑|button"],
                [],
            )
        )
        self.assertFalse(
            hierarchy_matches(
                hierarchy,
                ["编辑|button", "编辑|button"],
                [],
            )
        )

    def test_contract_contains_exact_r02_r14_set(self):
        contract = load_reduced_motion_contract()
        self.assertEqual(len(contract["transition_targets"]), 13)
        self.assertEqual(
            [item["id"].split("-", 1)[0] for item in contract["transition_targets"]],
            [f"R{index:02d}" for index in range(2, 15)],
        )

    def test_event_hierarchy_and_stable_start_are_bound(self):
        hierarchy = {
            "capture_start_uptime_ms": 1030,
            "capture_complete_uptime_ms": 1040,
            "root_window_id": 7,
            "nodes": [
                _node("0.0", "火种库", "android.widget.TextView", heading=True),
                _node("0.1", "已复原 0 / 0", "android.widget.TextView"),
                _node("0.2", "还没有文明火种", "android.widget.TextView"),
            ],
        }
        trace = {
            "schema_version": 1,
            "clock": "android.os.SystemClock.uptimeMillis",
            "transition_id": "R14-confirm-delete",
            "transition_action": "click",
            "selector_utf16_hex": "删除".encode("utf-16-be").hex(),
            "scope_selector_utf16_hex": "删除“静态验证”吗？此操作无法撤销。".encode(
                "utf-16-be"
            ).hex(),
            "action_dispatch_uptime_ms": 1000,
            "action_complete_uptime_ms": 1010,
            "events": [
                {
                    "callback_sequence": 1,
                    "event_time_ms": 1020,
                    "event_type": 2048,
                    "content_change_types": 1,
                    "window_id": 7,
                    "package": "dev.lilies.civilizationseed",
                    "source": {
                        "package": "dev.lilies.civilizationseed",
                        "window_id": 7,
                        "class": "android.widget.TextView",
                        "view_id": None,
                        "text": "火种库",
                        "content_description": "",
                    },
                    "hierarchy": hierarchy,
                }
            ],
            "frames": [
                {
                    "request_sequence": index,
                    "capture_start_uptime_ms": 800 + 100 * index,
                    "capture_complete_uptime_ms": 820 + 100 * index,
                    "pixel_buffer_sha256": "a" * 64,
                    "width": 1080,
                    "height": 2200,
                    "application_content_bounds": "[0,100][1080,2300]",
                }
                for index in range(13)
            ],
        }
        report = validate_transition_capture(trace)
        self.assertEqual(report["selected_callback_sequence"], 1)
        self.assertGreaterEqual(report["stable_frame_count"], 3)
        self.assertEqual(report["stable_start_uptime_ms"], 1100)

    def test_idle_capture_requires_26_identical_frames(self):
        trace = {
            "state_id": "R01-onboarding-idle",
            "before_hierarchy": {
                "nodes": [
                    _node("0.0", "文明火种", "android.widget.TextView", heading=True),
                    _node("0.1", "莉莉丝，闭着双眼，白发向两侧展开", "android.widget.ImageView"),
                    _node("0.2", "人类世界已经毁灭。", "android.widget.TextView"),
                    _node(
                        "0.3",
                        "莉莉丝是留下的最后一个人工智能。",
                        "android.widget.TextView",
                    ),
                    _node(
                        "0.4",
                        "她仍能搭建新的人工智能，并以一枚枚文明火种重塑世界。",
                        "android.widget.TextView",
                    ),
                    _node(
                        "0.5",
                        "完全离线运行，数据只保存在本机，不会上传。",
                        "android.widget.TextView",
                    ),
                    _node("0.6", "启动文明重建", "android.widget.Button"),
                ]
            },
            "frames": [
                {
                    "request_sequence": index,
                    "capture_start_uptime_ms": 1000 + 200 * index,
                    "capture_complete_uptime_ms": 1020 + 200 * index,
                    "pixel_buffer_sha256": "b" * 64,
                    "width": 1080,
                    "height": 2200,
                    "application_content_bounds": "[0,100][1080,2300]",
                }
                for index in range(26)
            ],
        }
        report = validate_idle_capture(trace)
        self.assertEqual(report["frame_count"], 26)


if __name__ == "__main__":
    unittest.main()
