import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from .constants import APPLICATION_ID
from .device import AndroidDevice
from .util import (
    OracleError,
    canonical_json_bytes,
    sanitize_logcat,
    sha256_file,
    write_new_or_replace,
)


_SOCKET_TABLES = ("tcp", "tcp6", "udp", "udp6")


def _target_uid(device: AndroidDevice) -> tuple[str, str]:
    package_dump = device.shell("dumpsys", "package", APPLICATION_ID).text()
    match = re.search(r"\buserId=(\d+)", package_dump)
    if not match:
        raise OracleError("package dump does not expose target uid")
    return match.group(1), package_dump


def capture_uid_socket_tables(
    device: AndroidDevice, uid: str
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    proc_net: dict[str, Any] = {}
    uid_socket_rows: dict[str, list[str]] = {}
    for table in _SOCKET_TABLES:
        result = device.shell("cat", f"/proc/net/{table}", check=False)
        content = result.text() if result.returncode == 0 else None
        matching_uid_rows = []
        if content is not None:
            for line in content.splitlines()[1:]:
                fields = line.split()
                if len(fields) > 7 and fields[7] == uid:
                    matching_uid_rows.append(line.strip())
        proc_net[table] = {
            "readable": result.returncode == 0,
            "sha256": hashlib.sha256(result.stdout).hexdigest()
            if result.returncode == 0
            else None,
            "content": content,
        }
        uid_socket_rows[table] = matching_uid_rows
    return proc_net, uid_socket_rows


class UidSocketIntervalObserver:
    """Continuously sample all target-UID socket tables across every launch."""

    def __init__(
        self, device: AndroidDevice, uid: str, *, interval_seconds: float = 0.1
    ):
        self.device = device
        self.uid = uid
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._samples: list[dict[str, Any]] = []
        self._errors: list[str] = []
        self._started_ns: int | None = None
        self._completed_ns: int | None = None

    def _sample(self) -> None:
        sampled_at = time.time_ns()
        try:
            proc_net, rows = capture_uid_socket_tables(self.device, self.uid)
            if any(not proc_net[table]["readable"] for table in _SOCKET_TABLES):
                raise OracleError("complete /proc UID socket evidence is unavailable")
            self._samples.append(
                {
                    "sampled_at_unix_ns": sampled_at,
                    "uid_socket_rows": rows,
                }
            )
        except Exception as error:  # fail closed after joining the sampler
            self._errors.append(f"{type(error).__name__}: {error}")
            self._stop.set()

    def _poll(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._sample()

    def start(self) -> None:
        if self._thread is not None:
            raise OracleError("UID socket interval observer was already started")
        self._started_ns = time.time_ns()
        self._sample()
        if self._errors:
            raise OracleError(self._errors[0])
        self._thread = threading.Thread(
            target=self._poll, name="t01m-uid-socket-observer", daemon=True
        )
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        if self._thread is None or self._started_ns is None:
            raise OracleError("UID socket interval observer was not started")
        self._stop.set()
        self._thread.join(timeout=10.0)
        if self._thread.is_alive():
            raise OracleError("UID socket interval observer did not terminate")
        self._sample()
        self._completed_ns = time.time_ns()
        if self._errors:
            raise OracleError(
                "UID socket interval observation failed: " + "; ".join(self._errors)
            )
        observations = [
            sample
            for sample in self._samples
            if any(sample["uid_socket_rows"][table] for table in _SOCKET_TABLES)
        ]
        return {
            "schema_version": 1,
            "application_id": APPLICATION_ID,
            "uid": self.uid,
            "started_at_unix_ns": self._started_ns,
            "completed_at_unix_ns": self._completed_ns,
            "sample_interval_milliseconds": int(self.interval_seconds * 1000),
            "sample_count": len(self._samples),
            "nonempty_observations": observations,
            "all_tables_readable": True,
        }


def capture_runtime_state(
    device: AndroidDevice,
    destination: Path,
    *,
    phase: str = "standalone",
    logcat_cursor_started_unix_ns: int | None = None,
) -> dict[str, Any]:
    if phase not in {"standalone", "prelaunch", "post_workload"}:
        raise OracleError("invalid runtime capture phase")
    uid, package_dump = _target_uid(device)
    netstats = device.shell("dumpsys", "netstats", "detail").text()
    uid_netstats = [
        line.strip()
        for line in netstats.splitlines()
        if re.search(rf"\buid={re.escape(uid)}\b", line)
    ]
    proc_net, uid_socket_rows = capture_uid_socket_tables(device, uid)
    logcat = device.adb_cmd("logcat", "-d", "-v", "epoch").text()
    sanitized = sanitize_logcat(logcat, device.serial)
    crash_markers = [
        marker
        for marker in (
            "FATAL EXCEPTION",
            "ANR in " + APPLICATION_ID,
            "Process: " + APPLICATION_ID,
        )
        if marker in sanitized
    ]
    package_log_lines = [
        line for line in sanitized.splitlines() if APPLICATION_ID in line
    ]
    socket_attempt_markers = [
        line
        for line in package_log_lines
        if re.search(
            r"(?i)\b(socket|connect(?:ion)?|dns|http|tls|network)\b", line
        )
    ]
    document = {
        "schema_version": 1,
        "application_id": APPLICATION_ID,
        "uid": uid,
        "phase": phase,
        "captured_at_unix_ns": time.time_ns(),
        "logcat_cursor_started_unix_ns": logcat_cursor_started_unix_ns,
        "airplane_mode_on": device.shell(
            "settings", "get", "global", "airplane_mode_on"
        ).text().strip(),
        "wifi_state": device.shell("cmd", "wifi", "status", check=False).text().strip(),
        "mobile_data_setting": device.shell(
            "settings", "get", "global", "mobile_data", check=False
        ).text().strip(),
        "package_dump_sha256": hashlib.sha256(package_dump.encode()).hexdigest(),
        "uid_netstats_lines": uid_netstats,
        "proc_net": proc_net,
        "uid_socket_rows": uid_socket_rows,
        "sanitized_logcat_sha256": hashlib.sha256(sanitized.encode()).hexdigest(),
        "crash_anr_markers": crash_markers,
        "package_log_lines": package_log_lines,
        "socket_attempt_markers": socket_attempt_markers,
    }
    write_new_or_replace(destination, canonical_json_bytes(document))
    return document


def compare_runtime_states(
    before_path: Path,
    after_path: Path,
    *,
    socket_interval_path: Path | None = None,
) -> dict[str, Any]:
    with before_path.open("r", encoding="utf-8") as source:
        before = json.load(source)
    with after_path.open("r", encoding="utf-8") as source:
        after = json.load(source)
    if before.get("application_id") != APPLICATION_ID or after.get(
        "application_id"
    ) != APPLICATION_ID:
        raise OracleError("runtime states are not bound to target application")
    if (
        not isinstance(before.get("uid"), str)
        or before.get("uid") != after.get("uid")
    ):
        raise OracleError("runtime states are not bound to one target UID")
    if socket_interval_path is not None and (
        before.get("phase") != "prelaunch"
        or after.get("phase") != "post_workload"
        or not isinstance(before.get("logcat_cursor_started_unix_ns"), int)
        or before.get("logcat_cursor_started_unix_ns")
        != after.get("logcat_cursor_started_unix_ns")
    ):
        raise OracleError("A07 runtime states do not span one pre-launch log cursor")
    if before.get("airplane_mode_on") != "1" or after.get("airplane_mode_on") != "1":
        raise OracleError("network was not disabled for the complete interval")
    for state in (before, after):
        if "disabled" not in state.get("wifi_state", "").lower():
            raise OracleError("Wi-Fi was not observably disabled")
        if state.get("mobile_data_setting") not in {"0", "null", ""}:
            raise OracleError("mobile data was not observably disabled")
    if after.get("crash_anr_markers"):
        raise OracleError("runtime log contains crash/ANR markers")
    if before.get("uid_netstats_lines") != after.get("uid_netstats_lines"):
        raise OracleError("target package traffic counters changed")
    before_sockets = before.get("uid_socket_rows")
    after_sockets = after.get("uid_socket_rows")
    if not isinstance(before_sockets, dict) or not isinstance(after_sockets, dict):
        raise OracleError("runtime state lacks package UID socket observations")
    expected_tables = {"tcp", "tcp6", "udp", "udp6"}
    if set(before_sockets) != expected_tables or set(after_sockets) != expected_tables:
        raise OracleError("runtime state lacks the complete UID socket table set")
    for state in (before, after):
        proc = state.get("proc_net")
        if (
            not isinstance(proc, dict)
            or set(proc) != expected_tables
            or any(
                not isinstance(proc[table], dict)
                or proc[table].get("readable") is not True
                for table in expected_tables
            )
        ):
            raise OracleError("complete /proc UID socket evidence is unavailable")
    for table in expected_tables:
        if before_sockets[table]:
            raise OracleError(f"pre-launch target UID already owns a {table} socket")
        if after_sockets[table]:
            raise OracleError(f"post-workload target UID owns a {table} socket")
    if before.get("socket_attempt_markers") or after.get("socket_attempt_markers"):
        raise OracleError("target logcat contains a socket/network attempt")
    interval_verified = False
    if socket_interval_path is not None:
        with socket_interval_path.open("r", encoding="utf-8") as source:
            interval = json.load(source)
        if (
            not isinstance(interval, dict)
            or interval.get("application_id") != APPLICATION_ID
            or interval.get("uid") != before["uid"]
            or interval.get("all_tables_readable") is not True
            or not isinstance(interval.get("sample_count"), int)
            or interval["sample_count"] < 2
            or not isinstance(interval.get("started_at_unix_ns"), int)
            or not isinstance(interval.get("completed_at_unix_ns"), int)
            or interval["started_at_unix_ns"]
            >= interval["completed_at_unix_ns"]
            or not isinstance(interval.get("nonempty_observations"), list)
        ):
            raise OracleError("UID socket interval evidence is incomplete")
        if interval["nonempty_observations"]:
            raise OracleError("target UID owned a socket during the launch interval")
        interval_verified = True
    return {
        "schema_version": 1,
        "before_sha256": sha256_file(before_path),
        "after_sha256": sha256_file(after_path),
        "traffic_counters_unchanged": True,
        "prelaunch_uid_sockets_absent": True,
        "post_workload_uid_sockets_absent": True,
        "uid_socket_interval_clean": interval_verified,
        "socket_attempts_absent": True,
        "crash_anr_free": True,
        "result": "pass",
    }


_BOUND_PATTERN = re.compile(r"^\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]$")
_CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def analyze_accessibility_hierarchy(
    hierarchy: dict[str, Any], *, density: float
) -> dict[str, Any]:
    if density <= 0:
        raise OracleError("display density must be positive")
    clickable = []
    failures = []
    for node in hierarchy.get("nodes", []):
        if (
            node.get("visible_to_user") is not True
            or not node.get("enabled")
            or not node.get("clickable")
        ):
            continue
        match = _BOUND_PATTERN.fullmatch(node.get("bounds", ""))
        if not match:
            raise OracleError("clickable node has invalid bounds")
        left, top, right, bottom = map(int, match.groups())
        width_dp = (right - left) / density
        height_dp = (bottom - top) / density
        text = node.get("text") or ""
        description = node.get("content_description") or ""
        label = text if _CJK_PATTERN.search(text) else description or text
        issues = []
        if width_dp < 48.0 or height_dp < 48.0:
            issues.append("touch_target_below_48dp")
        if not _CJK_PATTERN.search(label):
            issues.append("missing_chinese_semantic_name")
        if issues:
            failures.append({"path": node.get("path"), "issues": issues})
        clickable.append(
            {
                "path": node.get("path"),
                "label": label,
                "width_dp": width_dp,
                "height_dp": height_dp,
            }
        )
    return {
        "schema_version": 1,
        "enabled_clickable_nodes": clickable,
        "failures": failures,
        "result": "pass" if not failures else "fail",
    }


def verify_focus_sequence(
    trace: dict[str, Any], expected: list[dict[str, str]]
) -> dict[str, Any]:
    events = trace.get("focus_events")
    if not isinstance(events, list):
        raise OracleError("focus trace lacks focus_events")
    observed = [
        {
            "name": event.get("text") or event.get("content_description") or "",
            "class": event.get("class") or "",
        }
        for event in events
    ]
    if observed != expected:
        raise OracleError(
            f"TalkBack focus sequence mismatch: expected={expected!r}, observed={observed!r}"
        )
    return {"event_count": len(observed), "result": "pass"}


def _focus_role(event: dict[str, Any]) -> str:
    if event.get("heading") is True:
        return "heading"
    class_name = event.get("class", "")
    name = event.get("text") or event.get("content_description") or ""
    if name.startswith("火种：") and "；状态：" in name:
        return "group"
    if class_name.endswith("EditText"):
        return "edit_text"
    if class_name.endswith("ImageView"):
        return "image"
    if class_name.endswith("Spinner"):
        return "chooser"
    if event.get("checkable") or class_name.endswith(
        ("RadioButton", "CheckBox", "CheckedTextView")
    ):
        return "choice"
    if class_name.endswith(("Button", "ImageButton")) or event.get("clickable"):
        return "button"
    return "text"


def verify_focus_signature_cycle(
    trace: dict[str, Any], expected: list[str]
) -> dict[str, Any]:
    events = trace.get("focus_events")
    if not isinstance(events, list):
        raise OracleError("focus trace lacks focus_events")
    application_events = [
        event for event in events if event.get("package") == APPLICATION_ID
    ]
    observed = [
        f"{event.get('text') or event.get('content_description') or ''}|{_focus_role(event)}"
        for event in application_events
    ]
    required = expected + [expected[0]]
    gesture_states = trace.get("dumpsys_accessibility_after_each_gesture")
    if (
        not isinstance(gesture_states, list)
        or len(gesture_states) != len(required)
        or any(
            item.get("gesture_index") != index
            or item.get("talkback_present") is not True
            or item.get("touch_exploration_enabled") is not True
            or not isinstance(item.get("dumpsys_utf8"), str)
            or not item["dumpsys_utf8"]
            for index, item in enumerate(gesture_states)
        )
    ):
        raise OracleError("focus trace lacks complete TalkBack state after each gesture")
    if observed != required:
        raise OracleError(
            f"TalkBack focus cycle mismatch: expected={required!r}, observed={observed!r}"
        )
    return {
        "event_count": len(observed),
        "expected_cycle": required,
        "dumpsys_state_count": len(gesture_states),
        "result": "pass",
    }
