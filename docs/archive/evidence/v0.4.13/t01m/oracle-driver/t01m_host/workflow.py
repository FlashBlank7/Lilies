import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import load_flow
from .constants import APPLICATION_ID
from .device import AndroidDevice
from .driver import SemanticDriver
from .util import (
    OracleError,
    canonical_json_bytes,
    sanitize_logcat,
    sha256_file,
    write_new_or_replace,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _matches(nodes: list[dict[str, Any]], selector: str) -> list[dict[str, Any]]:
    return [
        node
        for node in nodes
        if node.get("text") == selector or node.get("content_description") == selector
    ]


def _scoped_nodes(
    nodes: list[dict[str, Any]], scope: str | None
) -> list[dict[str, Any]]:
    if scope is None:
        return nodes
    roots = _matches(nodes, scope)
    if len(roots) != 1:
        raise OracleError(f"scope must resolve uniquely, observed {len(roots)}: {scope!r}")
    prefix = roots[0]["path"] + "."
    return [node for node in nodes if node["path"] == roots[0]["path"] or node["path"].startswith(prefix)]


def _node_role(node: dict[str, Any]) -> str:
    if node.get("heading") is True:
        return "heading"
    class_name = node.get("class", "")
    if class_name.endswith("EditText"):
        return "edit_text"
    if class_name.endswith("ImageView"):
        return "image"
    if class_name.endswith("Spinner"):
        return "chooser"
    if node.get("checkable") or class_name.endswith(
        ("RadioButton", "CheckBox", "CheckedTextView")
    ):
        return "choice"
    if class_name.endswith(("Button", "ImageButton")) or node.get("clickable"):
        return "button"
    return "text"


def _semantic_name(node: dict[str, Any]) -> str:
    return node.get("text") or node.get("content_description") or ""


def _visible_record_cards(nodes: list[dict[str, Any]]) -> list[str]:
    return [
        name
        for node in nodes
        if (name := _semantic_name(node)).startswith("火种：")
        and "；类别：" in name
        and "；优先级：" in name
        and "；状态：" in name
    ]


def evaluate_assertion(
    hierarchy: dict[str, Any], step: dict[str, Any]
) -> dict[str, Any]:
    nodes = _scoped_nodes(hierarchy["nodes"], step.get("scope"))
    action = step["action"]
    assertion = step["assertion"]
    if action == "assert_visible":
        observed = len(_matches(nodes, assertion["text"]))
        passed = observed == assertion.get("count", 1)
        value = {"text": assertion["text"], "observed_count": observed}
    elif action == "assert_absent":
        observed = len(_matches(nodes, assertion["text"]))
        passed = observed == 0
        value = {"text": assertion["text"], "observed_count": observed}
    elif action == "assert_count":
        observed = [node.get("text", "") for node in nodes if node.get("text", "").startswith(assertion["prefix"])]
        passed = len(observed) == assertion["count"]
        value = {"prefix": assertion["prefix"], "observed": observed}
    elif action == "assert_order":
        expected = assertion["texts"]
        positions = []
        for text in expected:
            found = _matches(nodes, text)
            if len(found) != 1:
                positions.append(None)
            else:
                positions.append(tuple(int(part) for part in found[0]["path"].split(".")))
        passed = all(item is not None for item in positions) and positions == sorted(positions)
        if assertion.get("exact"):
            domain_role = assertion.get("domain_role")
            domain_prefix = assertion.get("domain_prefix")
            if domain_role is None and domain_prefix is None:
                raise OracleError("exact order requires an explicit semantic domain")
            relevant = []
            for node in nodes:
                name = _semantic_name(node)
                if domain_role is not None and _node_role(node) != domain_role:
                    continue
                if domain_prefix is not None and not name.startswith(domain_prefix):
                    continue
                if name:
                    relevant.append(name)
            passed = passed and relevant == expected
        value = {
            "expected": expected,
            "paths": positions,
            "exact_domain_observed": relevant if assertion.get("exact") else None,
        }
    elif action == "assert_record_set":
        expected = assertion["texts"]
        observed = _visible_record_cards(nodes)
        passed = observed == expected and len(observed) == len(set(observed))
        value = {"expected": expected, "observed": observed}
    elif action == "assert_offline_private":
        qualifiers = []
        exact = assertion.get(
            "text", "完全离线运行，数据只保存在本机，不会上传。"
        )
        for node in nodes:
            text = node.get("text") or node.get("content_description") or ""
            if text == exact:
                qualifiers.append(text)
        passed = len(qualifiers) == 1
        value = {"qualifying_texts": qualifiers}
    else:
        raise OracleError(f"not a local assertion: {action}")
    if not passed:
        raise OracleError(f"assertion failed: {value}")
    return value


class WorkflowRunner:
    def __init__(self, device: AndroidDevice, output: Path):
        self.device = device
        self.output = output
        self.artifacts = output / "artifacts"
        self.driver = SemanticDriver(device, self.artifacts)
        self.steps_root = self.artifacts / "steps"
        self.hierarchy_root = self.artifacts / "ui-hierarchy"
        self.screenshot_root = self.artifacts / "screenshots"

    def _hierarchy(self, step_number: int, phase: str) -> tuple[Path, dict[str, Any]]:
        name = f"s{step_number:03d}-{phase}.json"
        path = self.hierarchy_root / name
        self.driver.dump(name, path)
        with path.open("r", encoding="utf-8") as source:
            return path, json.load(source)

    def _capture_logcat(self, step_number: int) -> dict[str, Any]:
        raw = self.device.adb_cmd("logcat", "-d", "-v", "epoch").text()
        sanitized = sanitize_logcat(raw, self.device.serial)
        path = self.steps_root / f"s{step_number:03d}-logcat.txt"
        write_new_or_replace(path, sanitized.encode("utf-8"))
        markers = [
            marker
            for marker in (
                "FATAL EXCEPTION",
                "ANR in " + APPLICATION_ID,
                "Process: " + APPLICATION_ID,
            )
            if marker in sanitized
        ]
        if markers:
            raise OracleError(f"step logcat contains crash/ANR markers: {markers}")
        return {"path": path.as_posix(), "bytes": path.stat().st_size, "sha256": sha256_file(path)}

    def _perform(self, step: dict[str, Any], fixtures: dict[str, str]) -> dict[str, Any]:
        action = step["action"]
        if action.startswith("assert_"):
            raise AssertionError("local assertions are evaluated from after hierarchy")
        if action in {"click", "wait", "set_text"}:
            value = step.get("value")
            if "value_ref" in step:
                value = fixtures[step["value_ref"]]
            return self.driver.invoke(
                action,
                selector=step["selector"],
                selector_type=step.get("selector_type", "any"),
                scope=step.get("scope"),
                scope_type=step.get("scope_type", "any"),
                value=value,
            )
        if action == "set_text_utf16_hex":
            return self.driver.invoke(
                action,
                selector=step["selector"],
                selector_type=step.get("selector_type", "any"),
                scope=step.get("scope"),
                scope_type=step.get("scope_type", "any"),
                utf16_hex=step["value_utf16_hex"],
            )
        if action in {"back", "talkback_next"}:
            return self.driver.invoke(action)
        if action == "focus_trace":
            evidence_name = f"s{step['number']:03d}-focus.json"
            receipt = self.driver.invoke(
                action,
                evidence_name=evidence_name,
                extra={
                    "count": step.get("count", 1),
                    "interval_ms": step.get("interval_ms", 700),
                },
            )
            self.driver.copy_private_evidence(
                evidence_name, self.artifacts / "talkback-focus" / evidence_name
            )
            return receipt
        if action == "force_stop_relaunch":
            self.device.force_stop()
            self.device.launch()
            return {"status": "pass"}
        if action == "rotate":
            self.device.rotate(step["orientation"])
            return {"status": "pass"}
        if action in {"dump", "screenshot"}:
            return {"status": "pass"}
        raise OracleError(f"unsupported workflow action: {action}")

    def run(self, flow_path: Path, *, start: int = 1, stop: int | None = None) -> dict[str, Any]:
        flow = load_flow(flow_path)
        fixtures = flow["unicode_boundaries"]
        records = []
        self.artifacts.mkdir(parents=True, exist_ok=True)
        for step in flow["steps"]:
            number = step["number"]
            if number < start or (stop is not None and number > stop):
                continue
            self.device.adb_cmd("logcat", "-c")
            started_wall = _now()
            started_mono = time.monotonic_ns()
            before_path: Path | None = None
            after_path: Path | None = None
            result = "fail"
            observed: dict[str, Any] = {}
            screenshot: dict[str, Any] | None = None
            error: str | None = None
            evidence_errors: list[str] = []
            try:
                before_path, _ = self._hierarchy(number, "before")
                if step["action"].startswith("assert_"):
                    action_receipt = {"status": "pass", "kind": "local_hierarchy_assertion"}
                else:
                    action_receipt = self._perform(step, fixtures)
                after_path, after = self._hierarchy(number, "after")
                if step["action"].startswith("assert_"):
                    observed = evaluate_assertion(after, step)
                if step.get("screenshot"):
                    screenshot_name = step["screenshot"]
                    screenshot = self.driver.screenshot(
                        screenshot_name, self.screenshot_root / screenshot_name
                    )
                result = "pass"
            except Exception as caught:
                error = f"{type(caught).__name__}: {caught}"
                try:
                    after_path, _ = self._hierarchy(number, "after-failure")
                except Exception as evidence_error:
                    evidence_errors.append(
                        f"after_hierarchy: {type(evidence_error).__name__}: {evidence_error}"
                    )
                try:
                    failure_name = f"failure-s{number:03d}.png"
                    screenshot = self.driver.screenshot(
                        failure_name, self.screenshot_root / failure_name
                    )
                except Exception as evidence_error:
                    evidence_errors.append(
                        f"failure_screenshot: {type(evidence_error).__name__}: {evidence_error}"
                    )
                action_receipt = {"status": "fail"}
            ended_mono = time.monotonic_ns()
            try:
                logcat = self._capture_logcat(number)
            except Exception as evidence_error:
                evidence_errors.append(
                    f"logcat: {type(evidence_error).__name__}: {evidence_error}"
                )
                logcat = None
            if evidence_errors:
                result = "fail"
                evidence_summary = "; ".join(evidence_errors)
                error = (
                    f"{error}; evidence: {evidence_summary}"
                    if error
                    else f"evidence capture failed: {evidence_summary}"
                )

            def hierarchy_reference(path: Path | None) -> dict[str, Any] | None:
                if path is None:
                    return None
                return {"path": path.as_posix(), "sha256": sha256_file(path)}

            record = {
                "schema_version": 1,
                "step_number": number,
                "step_id": step["id"],
                "started_at": started_wall,
                "ended_at": _now(),
                "started_monotonic_ns": started_mono,
                "ended_monotonic_ns": ended_mono,
                "duration_ns": ended_mono - started_mono,
                "action": step["action"],
                "selector": step.get("selector"),
                "scope": step.get("scope"),
                "coordinates_used_for_pass": False,
                "before_hierarchy": hierarchy_reference(before_path),
                "after_hierarchy": hierarchy_reference(after_path),
                "assertion_inputs": step.get("assertion", {}),
                "observed_values": observed,
                "action_receipt": action_receipt,
                "sanitized_logcat": logcat,
                "screenshot": screenshot,
                "result": result,
                "error": error,
                "evidence_capture_errors": evidence_errors,
            }
            record_path = self.steps_root / f"s{number:03d}.json"
            write_new_or_replace(record_path, canonical_json_bytes(record))
            records.append({"path": record_path.as_posix(), "sha256": sha256_file(record_path), "result": result})
            if result != "pass":
                raise OracleError(f"A06 stopped at step {number}: {error}")
        trace = {
            "schema_version": 1,
            "case_id": "A06",
            "application_id": APPLICATION_ID,
            "step_count": len(records),
            "frozen_step_count": len(flow["steps"]),
            "complete_frozen_run": (
                start == 1
                and stop is None
                and len(records) == len(flow["steps"])
            ),
            "steps": records,
            "result": (
                "pass"
                if start == 1
                and stop is None
                and len(records) == len(flow["steps"])
                and all(item["result"] == "pass" for item in records)
                else "partial"
                if records and all(item["result"] == "pass" for item in records)
                else "fail"
            ),
        }
        trace_path = self.artifacts / "ui-flow-trace.json"
        write_new_or_replace(trace_path, canonical_json_bytes(trace))
        return trace
