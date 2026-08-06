import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from t01m_host.case_runners import PersistenceRunner


class _Device:
    def __init__(self):
        self.events = []

    def set_offline(self):
        self.events.append("offline")

    def clear_data(self):
        self.events.append("clear-data")

    def set_font_scale(self, value):
        self.events.append("font-scale")

    def set_animation_scales(self, value):
        self.events.append("animation-scales")

    def force_stop(self):
        self.events.append("force-stop")

    def adb_cmd(self, *args):
        self.events.append("logcat-clear")

    def snapshot_shared_storage(self):
        self.events.append("shared-storage")
        return []

    def launch(self):
        self.events.append("launch")


class _Observability:
    def __init__(self, events):
        self.events = events

    def capture(self, phase):
        self.events.append(f"daemon-{phase}")
        return {"phase": phase}


class _Observer:
    def __init__(self, device, uid):
        self.device = device
        self.uid = uid

    def start(self):
        self.device.events.append("socket-interval-start")


class A07OrderingTests(unittest.TestCase):
    def test_prelaunch_baseline_precedes_first_launch(self):
        device = _Device()

        def capture_runtime(fake_device, destination, **kwargs):
            fake_device.events.append("runtime-prelaunch")
            self.assertNotIn("launch", fake_device.events)
            self.assertEqual(kwargs["phase"], "prelaunch")
            return {
                "uid": "10123",
                "uid_socket_rows": {
                    "tcp": [],
                    "tcp6": [],
                    "udp": [],
                    "udp6": [],
                },
            }

        with tempfile.TemporaryDirectory() as temporary, patch(
            "t01m_host.case_runners.capture_runtime_state",
            side_effect=capture_runtime,
        ), patch(
            "t01m_host.case_runners.UidSocketIntervalObserver", _Observer
        ):
            runner = PersistenceRunner(device, Path(temporary))
            runner._prepare_prelaunch(_Observability(device.events))

        expected = [
            "offline",
            "clear-data",
            "font-scale",
            "animation-scales",
            "daemon-before",
            "force-stop",
            "logcat-clear",
            "runtime-prelaunch",
            "shared-storage",
            "shared-storage",
            "socket-interval-start",
        ]
        self.assertEqual(device.events, expected)
        self.assertNotIn("launch", device.events)


if __name__ == "__main__":
    unittest.main()
