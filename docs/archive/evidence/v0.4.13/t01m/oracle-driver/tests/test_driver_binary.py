import unittest
import zipfile
from pathlib import Path

from t01m_host.constants import DRIVER_APK, DRIVER_SHA256
from t01m_host.util import sha256_file


class DriverBinaryTests(unittest.TestCase):
    def test_frozen_binary_contains_separate_utf16_hex_action(self):
        self.assertEqual(sha256_file(DRIVER_APK), DRIVER_SHA256)
        with zipfile.ZipFile(DRIVER_APK) as archive:
            dex = archive.read("classes.dex")
        self.assertIn(b"set_text_utf16_hex", dex)
        self.assertIn(b"value_utf16_hex", dex)
        self.assertIn(b"observed_utf16_hex", dex)

    def test_source_never_places_raw_invalid_text_in_json_or_bundle(self):
        source = (
            Path(__file__).resolve().parent.parent
            / "src/dev/lilies/t01m/oracle/T01MOracleInstrumentation.java"
        ).read_text(encoding="utf-8")
        self.assertIn("putUtf8OrHex(item, \"text\", node.getText())", source)
        self.assertIn("putBundleUtf8OrHex(result, \"observed_text\"", source)
        self.assertNotIn(
            'item.put("text", bounded(asString(node.getText())))',
            source,
        )

    def test_binary_and_source_bind_final_frame_capture_actions(self):
        with zipfile.ZipFile(DRIVER_APK) as archive:
            dex = archive.read("classes.dex")
        for value in (
            b"transition_capture",
            b"idle_capture",
            b"normal_motion_capture",
            b"character_boxes",
            b"node_path",
            b"pixel_buffer_sha256",
            b"capture_start_uptime_ms",
            b"capture_complete_uptime_ms",
        ):
            self.assertIn(value, dex)
        source = (
            Path(__file__).resolve().parent.parent
            / "src/dev/lilies/t01m/oracle/T01MOracleInstrumentation.java"
        ).read_text(encoding="utf-8")
        capture_start = source.index(
            "long captureStart = SystemClock.uptimeMillis();"
        )
        screenshot = source.index("Bitmap screenshot = automation.takeScreenshot();")
        immutable_copy = source.index(
            "Bitmap content = cropped.copy(Bitmap.Config.ARGB_8888, false);"
        )
        capture_complete = source.index(
            "long captureComplete = SystemClock.uptimeMillis();"
        )
        self.assertLess(capture_start, screenshot)
        self.assertLess(screenshot, immutable_copy)
        self.assertLess(immutable_copy, capture_complete)

    def test_scope_and_atomic_output_are_fail_closed_in_source(self):
        source = (
            Path(__file__).resolve().parent.parent
            / "src/dev/lilies/t01m/oracle/T01MOracleInstrumentation.java"
        ).read_text(encoding="utf-8")
        card_guard = source.index('if (scopeSelector.startsWith("火种："))')
        dialog_guard = source.index(
            'scopeSelector.matches("^删除“.+”吗？此操作无法撤销。$")'
        )
        ancestor = source.index("AccessibilityNodeInfo dialogAncestor")
        self.assertLess(card_guard, dialog_guard)
        self.assertLess(dialog_guard, ancestor)
        self.assertIn("File.createTempFile(", source)
        self.assertNotIn('file.getName() + ".tmp"', source)


if __name__ == "__main__":
    unittest.main()
