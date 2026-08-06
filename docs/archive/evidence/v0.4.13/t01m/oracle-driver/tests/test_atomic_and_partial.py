import json
import tempfile
import unittest
from pathlib import Path

from t01m_host.constants import FLOW_CONFIG
from t01m_host.util import write_new_or_replace
from t01m_host.workflow import WorkflowRunner


class _Result:
    stdout = b""
    stderr = b""
    returncode = 0

    def text(self):
        return ""


class _Device:
    serial = "fixture"

    def adb_cmd(self, *args, **kwargs):
        return _Result()


class _PartialRunner(WorkflowRunner):
    def _hierarchy(self, step_number, phase):
        path = self.hierarchy_root / f"s{step_number:03d}-{phase}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "nodes": [
                        {
                            "path": "0.0",
                            "text": "文明火种",
                            "content_description": "",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return path, json.loads(path.read_text(encoding="utf-8"))


class AtomicAndPartialTests(unittest.TestCase):
    def test_successful_partial_flow_can_never_report_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = _PartialRunner(_Device(), Path(temporary)).run(
                FLOW_CONFIG, start=1, stop=1
            )
        self.assertEqual(report["step_count"], 1)
        self.assertFalse(report["complete_frozen_run"])
        self.assertEqual(report["result"], "partial")

    def test_atomic_writer_does_not_follow_a_predictable_tmp_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            victim = root / "victim.txt"
            victim.write_bytes(b"unchanged")
            predictable = root / ".result.json.tmp"
            predictable.symlink_to(victim)
            destination = root / "result.json"
            write_new_or_replace(destination, b"safe\n")
            self.assertEqual(victim.read_bytes(), b"unchanged")
            self.assertEqual(destination.read_bytes(), b"safe\n")
            self.assertTrue(predictable.is_symlink())


if __name__ == "__main__":
    unittest.main()
