import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .constants import APPLICATION_ID, REDUCED_MOTION_CONFIG
from .device import AndroidDevice
from .driver import SemanticDriver
from .util import OracleError, canonical_json_bytes, write_new_or_replace


def load_reduced_motion_contract(
    path: Path = REDUCED_MOTION_CONFIG,
) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as source:
        contract = json.load(source)
    if contract.get("schema_version") != 1:
        raise OracleError("unsupported reduced-motion contract schema")
    if contract.get("application_id") != APPLICATION_ID:
        raise OracleError("reduced-motion application id mismatch")
    targets = contract.get("transition_targets")
    if not isinstance(targets, list) or [item.get("id") for item in targets] != [
        f"R{index:02d}-" + suffix
        for index, suffix in (
            (2, "onboarding-to-empty-library"),
            (3, "open-create-form"),
            (4, "save-created-seed"),
            (5, "open-edit-form"),
            (6, "save-edited-notes"),
            (7, "open-filter-panel"),
            (8, "select-survival-category-filter"),
            (9, "return-to-filtered-library"),
            (10, "advance-to-restoring"),
            (11, "open-delete-confirmation-first"),
            (12, "cancel-delete"),
            (13, "open-delete-confirmation-second"),
            (14, "confirm-delete"),
        )
    ]:
        raise OracleError("R02-R14 target IDs are not frozen-complete")
    for target in targets:
        if not target.get("required") or not isinstance(target.get("absent"), list):
            raise OracleError(f"transition target is incomplete: {target.get('id')}")
        for signature in target["required"] + target["absent"]:
            parse_signature(signature)
    return contract


def parse_signature(value: str) -> tuple[str, str, bool]:
    if not isinstance(value, str):
        raise OracleError("transition signature must be text")
    parts = value.split("|")
    if len(parts) not in (2, 3) or not parts[0] or not parts[1]:
        raise OracleError(f"invalid transition signature: {value!r}")
    selected = False
    if len(parts) == 3:
        if parts[2] != "selected=true":
            raise OracleError(f"invalid signature predicate: {value!r}")
        selected = True
    return parts[0], parts[1], selected


def android_role(node: dict[str, Any]) -> str:
    if node.get("heading") is True:
        return "heading"
    class_name = node.get("class", "")
    label = node.get("text") or node.get("content_description") or ""
    if label.startswith("火种：") and "；状态：" in label:
        return "group"
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


def canonical_hierarchy(hierarchy: dict[str, Any]) -> bytes:
    nodes = hierarchy.get("nodes")
    if not isinstance(nodes, list):
        raise OracleError("hierarchy lacks nodes")
    canonical = []
    for node in nodes:
        required = (
            "path",
            "window_id",
            "class",
            "text",
            "content_description",
            "checked",
            "selected",
            "enabled",
            "bounds",
        )
        if any(key not in node for key in required):
            raise OracleError("canonical hierarchy node lacks required field")
        canonical.append(
            {
                "window_id": node["window_id"],
                "class": node["class"],
                "role": android_role(node),
                "text": node["text"],
                "content_description": node["content_description"],
                "checked": node["checked"],
                "selected": node["selected"],
                "enabled": node["enabled"],
                "bounds": node["bounds"],
            }
        )
    return canonical_json_bytes(canonical)


def hierarchy_matches(
    hierarchy: dict[str, Any], required: list[str], absent: list[str]
) -> bool:
    nodes = hierarchy.get("nodes", [])
    signatures = []
    for node in nodes:
        name = node.get("text") or node.get("content_description") or ""
        if not name:
            continue
        signatures.append((name, android_role(node), node.get("selected") is True))
    required_counts = Counter(parse_signature(encoded) for encoded in required)
    for (name, role, selected), expected_count in required_counts.items():
        observed_count = sum(
            item_name == name
            and item_role == role
            and (not selected or item_selected)
            for item_name, item_role, item_selected in signatures
        )
        if observed_count != expected_count:
            return False
    for encoded in absent:
        name, role, selected = parse_signature(encoded)
        count = sum(
            item_name == name
            and item_role == role
            and (not selected or item_selected)
            for item_name, item_role, item_selected in signatures
        )
        if count != 0:
            return False
    return True


def _candidate_event(
    event: dict[str, Any],
    hierarchy: dict[str, Any],
    dispatch_ms: int,
) -> bool:
    required = (
        "callback_sequence",
        "event_time_ms",
        "event_type",
        "content_change_types",
        "window_id",
        "package",
        "source",
    )
    if any(key not in event for key in required):
        raise OracleError("event lacks a required predicate field")
    if event["package"] != APPLICATION_ID:
        return False
    if event["event_type"] not in (32, 2048):
        return False
    if event["event_time_ms"] < dispatch_ms:
        return False
    root_window = hierarchy.get("root_window_id")
    if root_window is None:
        raise OracleError("candidate hierarchy lacks root_window_id")
    source = event["source"]
    if event["event_type"] == 2048:
        if not isinstance(source, dict):
            return False
        if any(
            key not in source
            for key in (
                "package",
                "window_id",
                "class",
                "view_id",
                "text",
                "content_description",
            )
        ):
            raise OracleError("content-change event source lacks retained fields")
        return (
            source.get("package") == APPLICATION_ID
            and source.get("window_id") == root_window
        )
    if source is None:
        return event["window_id"] == root_window
    if not isinstance(source, dict):
        raise OracleError("event source is neither object nor null")
    if any(
        key not in source
        for key in (
            "package",
            "window_id",
            "class",
            "view_id",
            "text",
            "content_description",
        )
    ):
        raise OracleError("window-state event source lacks retained fields")
    return (
        source.get("package") == APPLICATION_ID
        and source.get("window_id") == root_window
    )


def validate_transition_capture(
    trace: dict[str, Any],
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    contract = contract or load_reduced_motion_contract()
    target_by_id = {
        item["id"]: item for item in contract["transition_targets"]
    }
    transition_id = trace.get("transition_id")
    if transition_id not in target_by_id:
        raise OracleError("trace transition_id is not R02-R14")
    target = target_by_id[transition_id]
    action = target["action"]
    if trace.get("transition_action") != action["kind"]:
        raise OracleError("trace action kind does not match frozen target")
    if action["kind"] == "click":
        selector_hex = action["selector"].encode("utf-16-be").hex()
        if trace.get("selector_utf16_hex") != selector_hex:
            raise OracleError("trace selector does not match frozen target")
        expected_scope = action.get("scope")
        observed_scope_hex = trace.get("scope_selector_utf16_hex")
        if expected_scope is None:
            if observed_scope_hex is not None:
                raise OracleError("unexpected scoped selector in global transition")
        elif observed_scope_hex != expected_scope.encode("utf-16-be").hex():
            raise OracleError("trace scope does not match frozen target")
    dispatch = trace.get("action_dispatch_uptime_ms")
    complete = trace.get("action_complete_uptime_ms")
    if not isinstance(dispatch, int) or not isinstance(complete, int) or complete < dispatch:
        raise OracleError("action dispatch/complete timestamps are invalid")
    if trace.get("clock") != "android.os.SystemClock.uptimeMillis":
        raise OracleError("wall clock cannot bind reduced-motion events")
    events = trace.get("events")
    if not isinstance(events, list) or not events:
        raise OracleError("transition trace has no accessibility event stream")
    sequences = [event.get("callback_sequence") for event in events]
    if (
        any(not isinstance(value, int) for value in sequences)
        or sequences != sorted(sequences)
        or len(sequences) != len(set(sequences))
    ):
        raise OracleError("callback sequence is not strictly increasing")
    qualifying = []
    for event in events:
        potential_candidate = (
            event.get("package") == APPLICATION_ID
            and event.get("event_type") in (32, 2048)
            and isinstance(event.get("event_time_ms"), int)
            and event["event_time_ms"] >= dispatch
        )
        if not potential_candidate:
            continue
        hierarchy = event.get("hierarchy")
        if not isinstance(hierarchy, dict):
            raise OracleError("candidate event lacks synchronously captured hierarchy")
        start = hierarchy.get("capture_start_uptime_ms")
        end = hierarchy.get("capture_complete_uptime_ms")
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or end < start
        ):
            raise OracleError("hierarchy capture timestamps are invalid")
        if _candidate_event(event, hierarchy, dispatch) and hierarchy_matches(
            hierarchy, target["required"], target["absent"]
        ):
            qualifying.append((event, hierarchy))
    if not qualifying:
        raise OracleError("no candidate event hierarchy matches transition target")
    selected_event, selected_hierarchy = qualifying[0]
    capture_start = selected_hierarchy["capture_start_uptime_ms"]
    capture_complete = selected_hierarchy["capture_complete_uptime_ms"]
    if (
        capture_start < selected_event["event_time_ms"]
        or capture_start - selected_event["event_time_ms"]
        > contract["target_capture_start_tolerance_ms"]
    ):
        raise OracleError("selected hierarchy is not tightly bound to event")
    if selected_event["event_time_ms"] > (
        complete + contract["target_timeout_after_action_complete_ms"]
    ):
        raise OracleError("target event arrived after frozen timeout")
    selected_digest = hashlib.sha256(
        canonical_hierarchy(selected_hierarchy)
    ).hexdigest()
    for event, hierarchy in qualifying[1:]:
        if hierarchy["capture_start_uptime_ms"] > capture_complete:
            break
        digest = hashlib.sha256(canonical_hierarchy(hierarchy)).hexdigest()
        if digest != selected_digest:
            raise OracleError(
                "second different matching hierarchy arrived before selected capture completed"
            )
    frames = trace.get("frames")
    if not isinstance(frames, list) or len(frames) != contract["frame_count"]:
        raise OracleError("transition requires exactly 13 frames")
    starts: list[int] = []
    completes: list[int] = []
    sequences: list[int] = []
    for index, frame in enumerate(frames):
        start = frame.get("capture_start_uptime_ms")
        frame_complete = frame.get("capture_complete_uptime_ms")
        sequence = frame.get("request_sequence")
        digest = frame.get("pixel_buffer_sha256")
        if (
            not isinstance(start, int)
            or not isinstance(frame_complete, int)
            or frame_complete < start
            or not isinstance(sequence, int)
            or sequence != index
            or not isinstance(digest, str)
            or len(digest) != 64
            or not isinstance(frame.get("width"), int)
            or not isinstance(frame.get("height"), int)
            or not isinstance(frame.get("application_content_bounds"), str)
        ):
            raise OracleError("frame lacks a unique ordered capture-start/complete pair")
        starts.append(start)
        completes.append(frame_complete)
        sequences.append(sequence)
    if len(set(zip(starts, completes))) != len(frames):
        raise OracleError("transition reused a frame capture-start/complete pair")
    for previous, current in zip(starts, starts[1:]):
        spacing = current - previous
        if abs(spacing - contract["frame_interval_ms"]) > contract[
            "timestamp_tolerance_ms"
        ]:
            raise OracleError("successive transition capture-start spacing exceeds 25ms")
    tolerance = contract["timestamp_tolerance_ms"]
    if (
        abs((dispatch - starts[0]) - 200) > tolerance
        or not starts[1] < dispatch <= starts[2]
        or abs((starts[-1] - dispatch) - 1000) > tolerance
    ):
        raise OracleError(
            "transition frames do not span exactly dispatch-200ms through dispatch+1000ms"
        )
    geometry = {
        (
            frame["width"],
            frame["height"],
            frame["application_content_bounds"],
        )
        for frame in frames
    }
    if len(geometry) != 1:
        raise OracleError("application-content geometry changed during transition")
    stable = [
        frame
        for frame in frames
        if frame["capture_start_uptime_ms"] >= capture_complete
    ]
    if len(stable) < contract["stable_frame_minimum"]:
        raise OracleError("stable_start has fewer than three frames")
    stable_digest = stable[0]["pixel_buffer_sha256"]
    if any(
        frame["pixel_buffer_sha256"] != stable_digest
        for frame in stable[1:]
    ):
        raise OracleError("frames at/after stable_start are not byte-identical")
    return {
        "schema_version": 1,
        "transition_id": transition_id,
        "selected_callback_sequence": selected_event["callback_sequence"],
        "selected_hierarchy_sha256": selected_digest,
        "selected_capture_complete_uptime_ms": capture_complete,
        "stable_start_request_sequence": stable[0]["request_sequence"],
        "stable_start_uptime_ms": stable[0]["capture_start_uptime_ms"],
        "stable_frame_count": len(stable),
        "result": "pass",
    }


def validate_idle_capture(
    trace: dict[str, Any],
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    contract = contract or load_reduced_motion_contract()
    state_id = trace.get("state_id")
    hierarchy = trace.get("before_hierarchy")
    if not isinstance(hierarchy, dict):
        raise OracleError("idle capture lacks its bound initial hierarchy")
    if state_id == "R01-onboarding-idle":
        state = contract["initial_state"]
    elif state_id == "R04-populated-library-idle":
        state = next(
            item
            for item in contract["transition_targets"]
            if item["id"] == "R04-save-created-seed"
        )
    else:
        raise OracleError("idle capture state_id is not frozen")
    if not hierarchy_matches(hierarchy, state["required"], state["absent"]):
        raise OracleError("idle capture hierarchy does not match frozen state")
    frames = trace.get("frames")
    expected_count = contract["idle_frame_count"]
    if not isinstance(frames, list) or len(frames) != expected_count:
        raise OracleError("idle capture requires exactly 26 frames")
    starts: list[int] = []
    pairs: list[tuple[int, int]] = []
    digests: list[str] = []
    geometry = set()
    for index, frame in enumerate(frames):
        start = frame.get("capture_start_uptime_ms")
        complete = frame.get("capture_complete_uptime_ms")
        digest = frame.get("pixel_buffer_sha256")
        if (
            frame.get("request_sequence") != index
            or not isinstance(start, int)
            or not isinstance(complete, int)
            or complete < start
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise OracleError("idle frame has invalid capture binding")
        starts.append(start)
        pairs.append((start, complete))
        digests.append(digest)
        geometry.add(
            (
                frame.get("width"),
                frame.get("height"),
                frame.get("application_content_bounds"),
            )
        )
    if len(set(pairs)) != expected_count:
        raise OracleError("idle capture reused a frame request")
    for previous, current in zip(starts, starts[1:]):
        if abs((current - previous) - contract["idle_frame_interval_ms"]) > contract[
            "timestamp_tolerance_ms"
        ]:
            raise OracleError("successive idle capture-start spacing exceeds 25ms")
    if len(geometry) != 1:
        raise OracleError("idle application-content geometry changed")
    if len(set(digests)) != 1:
        raise OracleError("idle reduced-motion frames are not pixel-identical")
    return {
        "schema_version": 1,
        "state_id": state_id,
        "frame_count": expected_count,
        "pixel_buffer_sha256": digests[0],
        "result": "pass",
    }


class ReducedMotionRunner:
    """Execute and bind the exact R02-R14 reduced-motion transition flow."""

    def __init__(self, device: AndroidDevice, output: Path):
        self.device = device
        self.output = output
        self.driver = SemanticDriver(device, output / "artifacts")
        self.contract = load_reduced_motion_contract()

    def run(self) -> dict[str, Any]:
        self.device.clear_data()
        self.device.set_animation_scales(0.0)
        self.device.launch()
        transitions_root = (
            self.output / "artifacts" / "reduced-motion-frames"
        )
        reports = []
        idle_reports = []
        all_frame_bindings = []
        onboarding_idle = self.driver.capture_idle(
            state_id="R01-onboarding-idle",
            trace_name="R01-onboarding-idle.json",
            destination=transitions_root / "R01-onboarding-idle",
        )
        idle_reports.append(
            {
                "state_id": "R01-onboarding-idle",
                "trace_file": onboarding_idle["trace_file"],
                "validation": validate_idle_capture(
                    onboarding_idle["trace"], self.contract
                ),
            }
        )
        all_frame_bindings.extend(
            {
                "capture_id": "R01-onboarding-idle",
                "request_sequence": frame["request_sequence"],
                "pixel_buffer_sha256": frame["pixel_buffer_sha256"],
                "png_sha256": frame["sha256"],
            }
            for frame in onboarding_idle["trace"]["frames"]
        )
        onboarding_frame_name = onboarding_idle["trace"]["frames"][0]["evidence_name"]
        write_new_or_replace(
            self.output / "artifacts" / "screenshots" / "07_reduced_motion_a.png",
            (
                transitions_root
                / "R01-onboarding-idle"
                / onboarding_frame_name
            ).read_bytes(),
        )
        for target in self.contract["transition_targets"]:
            transition_id = target["id"]
            if transition_id == "R04-save-created-seed":
                self.driver.invoke(
                    "set_text", selector="火种名称", value="静态验证"
                )
                self.driver.invoke("click", selector="类别")
                self.driver.invoke("click", selector="生存基础")
                self.driver.invoke("click", selector="优先级")
                self.driver.invoke("click", selector="3")
            elif transition_id == "R06-save-edited-notes":
                self.driver.invoke("set_text", selector="备注", value="静默层")
            action = target["action"]
            capture = self.driver.capture_transition(
                transition_id=transition_id,
                action_kind=action["kind"],
                selector=action.get("selector"),
                scope=action.get("scope"),
                trace_name=transition_id + ".json",
                destination=transitions_root / transition_id,
            )
            report = validate_transition_capture(
                capture["trace"], self.contract
            )
            reports.append(
                {
                    "transition_id": transition_id,
                    "trace_file": capture["trace_file"],
                    "validation": report,
                }
            )
            all_frame_bindings.extend(
                {
                    "capture_id": transition_id,
                    "request_sequence": frame["request_sequence"],
                    "pixel_buffer_sha256": frame["pixel_buffer_sha256"],
                    "png_sha256": frame["sha256"],
                }
                for frame in capture["trace"]["frames"]
            )
            if transition_id == "R04-save-created-seed":
                populated_idle = self.driver.capture_idle(
                    state_id="R04-populated-library-idle",
                    trace_name="R04-populated-library-idle.json",
                    destination=transitions_root / "R04-populated-library-idle",
                )
                idle_reports.append(
                    {
                        "state_id": "R04-populated-library-idle",
                        "trace_file": populated_idle["trace_file"],
                        "validation": validate_idle_capture(
                            populated_idle["trace"], self.contract
                        ),
                    }
                )
                all_frame_bindings.extend(
                    {
                        "capture_id": "R04-populated-library-idle",
                        "request_sequence": frame["request_sequence"],
                        "pixel_buffer_sha256": frame["pixel_buffer_sha256"],
                        "png_sha256": frame["sha256"],
                    }
                    for frame in populated_idle["trace"]["frames"]
                )
                populated_frame_name = populated_idle["trace"]["frames"][0][
                    "evidence_name"
                ]
                write_new_or_replace(
                    self.output
                    / "artifacts"
                    / "screenshots"
                    / "08_reduced_motion_b.png",
                    (
                        transitions_root
                        / "R04-populated-library-idle"
                        / populated_frame_name
                    ).read_bytes(),
                )
        result = {
            "schema_version": 1,
            "case_id": "A09-reduced-motion",
            "transition_count": len(reports),
            "transitions": reports,
            "idle_capture_count": len(idle_reports),
            "idle_captures": idle_reports,
            "retained_frame_count": len(all_frame_bindings),
            "frame_set_sha256": hashlib.sha256(
                canonical_json_bytes(all_frame_bindings)
            ).hexdigest(),
            "frame_bindings": all_frame_bindings,
            "result": "pass",
        }
        write_new_or_replace(
            self.output / "artifacts" / "reduced-motion-report.json",
            canonical_json_bytes(result),
        )
        return result
