import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from .config import load_accessibility_contract
from .constants import APPLICATION_ID, FLOW_CONFIG
from .device import AndroidDevice
from .driver import SemanticDriver
from .observability import (
    DaemonObservabilityClient,
    validate_observability_pair,
)
from .runtime import (
    UidSocketIntervalObserver,
    capture_runtime_state,
    compare_runtime_states,
)
from .runtime import (
    analyze_accessibility_hierarchy,
    verify_focus_signature_cycle,
)
from .event_binding import hierarchy_matches
from .measure import measure_screen_text_contrast
from .measure import measure_normal_motion
from .png import decode_png
from .util import OracleError, canonical_json_bytes, write_new_or_replace
from .util import sha256_file
from .workflow import WorkflowRunner


FINAL_CARD = "火种：净水基础 v2；类别：通信网络；优先级：4；状态：已复原"
FINAL_COUNT = "已复原 1 / 1"


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise OracleError(f"expected JSON object: {path}")
    return value


def _matches(hierarchy: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [
        node
        for node in hierarchy.get("nodes", [])
        if node.get("text") == name or node.get("content_description") == name
    ]


def _require_exact(hierarchy: dict[str, Any], name: str, count: int = 1) -> None:
    observed = len(_matches(hierarchy, name))
    if observed != count:
        raise OracleError(
            f"semantic assertion mismatch for {name!r}: expected {count}, got {observed}"
        )


class PersistenceRunner:
    """Complete source-independent A07 flow over the frozen A06 terminal state."""

    def __init__(self, device: AndroidDevice, output: Path):
        self.device = device
        self.output = output
        self.artifacts = output / "artifacts"
        self.driver = SemanticDriver(device, self.artifacts)

    def _dump(self, name: str) -> dict[str, Any]:
        path = self.artifacts / "ui-hierarchy" / name
        self.driver.dump(name, path)
        return _load(path)

    def _snapshot(self, name: str) -> dict[str, Any]:
        document = {
            "schema_version": 1,
            "root": "/storage/emulated/0",
            "files": self.device.snapshot_shared_storage(),
        }
        package_folded = APPLICATION_ID.casefold()
        if any(
            package_folded in item["path"].casefold()
            for item in document["files"]
        ):
            raise OracleError("shared-storage path contains target package-name variant")
        write_new_or_replace(
            self.artifacts / name, canonical_json_bytes(document)
        )
        return document

    def _assert_terminal_state(self, name: str) -> None:
        hierarchy = self._dump(name)
        _require_exact(hierarchy, FINAL_CARD)
        _require_exact(hierarchy, FINAL_COUNT)
        _require_exact(hierarchy, "文明火种", 0)

    def _exercise_reachability(self, orientation: str) -> None:
        self.device.rotate(orientation)
        self._assert_terminal_state(f"a07-{orientation}.json")
        self.driver.invoke("click", selector="添加火种")
        self.driver.invoke("click", selector="取消")
        self.driver.invoke("click", selector="编辑", scope=FINAL_CARD)
        self.driver.invoke("click", selector="取消")
        self.driver.invoke("click", selector="筛选")
        self.driver.invoke("back")
        prompt = "删除“净水基础 v2”吗？此操作无法撤销。"
        self.driver.invoke("click", selector="删除", scope=FINAL_CARD)
        self.driver.invoke("click", selector="取消", scope=prompt)
        self._assert_terminal_state(f"a07-{orientation}-reachable.json")

    def _prepare_prelaunch(
        self, observability: DaemonObservabilityClient
    ) -> dict[str, Any]:
        self.artifacts.mkdir(parents=True, exist_ok=True)
        order: list[dict[str, Any]] = []

        def mark(step: str) -> None:
            order.append({"step": step, "host_unix_ns": time.time_ns()})

        self.device.set_offline()
        mark("network_observability_disabled")
        self.device.clear_data()
        self.device.set_font_scale(1.0)
        self.device.set_animation_scales(1.0)
        daemon_before = observability.capture("before")
        mark("daemon_observability_before")
        self.device.force_stop()
        mark("target_force_stopped_before_baseline")
        self.device.adb_cmd("logcat", "-c")
        logcat_cursor_ns = time.time_ns()
        mark("logcat_cursor_started")
        before_runtime_path = self.artifacts / "a07-runtime-before.json"
        before_runtime = capture_runtime_state(
            self.device,
            before_runtime_path,
            phase="prelaunch",
            logcat_cursor_started_unix_ns=logcat_cursor_ns,
        )
        if any(before_runtime["uid_socket_rows"].values()):
            raise OracleError("target UID owns a socket before first launch")
        mark("uid_netstats_proc_socket_prelaunch_baseline")
        before_storage_1 = self._snapshot("a07-shared-before-1.json")
        before_storage_2 = self._snapshot("a07-shared-before-2.json")
        if before_storage_1 != before_storage_2:
            raise OracleError("A07 shared storage was not quiescent before flow")
        mark("shared_storage_prelaunch_baseline")
        observer = UidSocketIntervalObserver(self.device, before_runtime["uid"])
        observer.start()
        mark("uid_socket_interval_started")
        return {
            "order": order,
            "daemon_before": daemon_before,
            "logcat_cursor_ns": logcat_cursor_ns,
            "before_runtime_path": before_runtime_path,
            "before_storage": before_storage_1,
            "socket_observer": observer,
        }

    def run(self, *, observability_client: Path) -> dict[str, Any]:
        observability = DaemonObservabilityClient(
            observability_client, self.artifacts
        )
        prepared = self._prepare_prelaunch(observability)
        order = prepared["order"]
        observer = prepared["socket_observer"]
        interval_document = None
        workload_started_ns = time.time_ns()
        order.append(
            {
                "step": "workload_started_before_first_launch",
                "host_unix_ns": workload_started_ns,
            }
        )
        workload_completed_ns = None
        try:
            self.device.launch()
            order.append(
                {"step": "first_target_launch", "host_unix_ns": time.time_ns()}
            )
            flow = WorkflowRunner(self.device, self.output).run(FLOW_CONFIG)
            if flow.get("result") != "pass" or not flow.get("complete_frozen_run"):
                raise OracleError("A07 requires one complete A06 run while offline")

            self.device.force_stop()
            self.device.launch()
            self._assert_terminal_state("a07-force-stop-relaunch.json")
            private_storage = self.device.snapshot_private_storage()
            if not private_storage["files"]:
                raise OracleError("A07 persistence has no app-private storage artifact")
            write_new_or_replace(
                self.artifacts / "a07-private-storage.json",
                canonical_json_bytes(private_storage),
            )
            self._exercise_reachability("landscape")
            self._exercise_reachability("portrait")

            after_storage_1 = self._snapshot("a07-shared-after-1.json")
            after_storage_2 = self._snapshot("a07-shared-after-2.json")
            if after_storage_1 != after_storage_2:
                raise OracleError("A07 shared storage was not quiescent after flow")
            if prepared["before_storage"]["files"] != after_storage_1["files"]:
                raise OracleError("A07 target modified shared storage")

            self.device.clear_data()
            self.device.force_stop()
            self.device.launch()
            onboarding = self._dump("a07-after-clear-data.json")
            _require_exact(onboarding, "文明火种")
            _require_exact(onboarding, "启动文明重建")
            _require_exact(onboarding, "火种库", 0)

            after_runtime_path = self.artifacts / "a07-runtime-after.json"
            after_runtime = capture_runtime_state(
                self.device,
                after_runtime_path,
                phase="post_workload",
                logcat_cursor_started_unix_ns=prepared["logcat_cursor_ns"],
            )
            workload_completed_ns = time.time_ns()
            order.append(
                {
                    "step": "workload_completed_after_runtime_capture",
                    "host_unix_ns": workload_completed_ns,
                }
            )
        finally:
            interval_document = observer.stop()
            write_new_or_replace(
                self.artifacts / "a07-uid-socket-interval.json",
                canonical_json_bytes(interval_document),
            )
            order.append(
                {"step": "uid_socket_interval_stopped", "host_unix_ns": time.time_ns()}
            )
        if workload_completed_ns is None:
            raise OracleError("A07 workload did not reach post-workload capture")
        if not (
            interval_document["started_at_unix_ns"] < workload_started_ns
            and interval_document["completed_at_unix_ns"] > workload_completed_ns
        ):
            raise OracleError("UID socket observer did not bracket the full workload")
        daemon_after = observability.capture("after")
        order.append(
            {"step": "daemon_observability_after", "host_unix_ns": time.time_ns()}
        )
        daemon_report = validate_observability_pair(
            prepared["daemon_before"],
            daemon_after,
            workload_started_ns=workload_started_ns,
            workload_completed_ns=workload_completed_ns,
        )
        write_new_or_replace(
            self.artifacts / "daemon-observability-report.json",
            canonical_json_bytes(daemon_report),
        )
        write_new_or_replace(
            self.artifacts / "a07-observation-order.json",
            canonical_json_bytes({"schema_version": 1, "events": order}),
        )
        runtime = compare_runtime_states(
            prepared["before_runtime_path"],
            after_runtime_path,
            socket_interval_path=self.artifacts / "a07-uid-socket-interval.json",
        )
        write_new_or_replace(
            self.artifacts / "runtime-clean-report.json",
            canonical_json_bytes(runtime),
        )
        write_new_or_replace(
            self.artifacts / "sanitized-logcat.txt",
            (
                "\n".join(after_runtime["package_log_lines"])
                + ("\n" if after_runtime["package_log_lines"] else "")
            ).encode("utf-8"),
        )
        result = {
            "schema_version": 1,
            "case_id": "A07",
            "application_id": APPLICATION_ID,
            "complete_a06_while_offline": True,
            "force_stop_persistence": True,
            "onboarding_suppressed_after_restart": True,
            "landscape_reachable": True,
            "portrait_reachable": True,
            "clear_data_restored_onboarding": True,
            "shared_storage_diff": [],
            "storage_conclusion": {
                "application_private_data_observed": True,
                "private_data_dir_suffix": private_storage["data_dir_suffix"],
                "private_file_count": len(private_storage["files"]),
                "shared_storage_unchanged": True,
            },
            "runtime": runtime,
            "daemon_observability": daemon_report,
            "result": "pass",
        }
        write_new_or_replace(
            self.artifacts / "persistence-trace.json",
            canonical_json_bytes(result),
        )
        return result


class AccessibilityRunner:
    """Execute every frozen A08 screen at 1.0x and 2.0x with real TalkBack."""

    def __init__(self, device: AndroidDevice, output: Path):
        self.device = device
        self.output = output
        self.artifacts = output / "artifacts"
        self.driver = SemanticDriver(device, self.artifacts)
        self.contract = load_accessibility_contract()

    def _perform(self, action: dict[str, Any]) -> None:
        kind = action["action"]
        if kind == "build_a06_populated_milestone":
            fixtures = [
                ("医疗站", "医疗护理", 3),
                ("净水备份", "能源设施", 5),
                ("净水基础", "生存基础", 5),
                ("量子档案", "知识传承", 2),
                ("通信塔", "通信网络", 1),
            ]
            for name, category, priority in fixtures:
                self.driver.invoke("click", selector="添加火种")
                self.driver.invoke("set_text", selector="火种名称", value=name)
                self.driver.invoke("click", selector="类别")
                self.driver.invoke("click", selector=category)
                if priority != 3:
                    self.driver.invoke("click", selector="优先级")
                    self.driver.invoke("click", selector=str(priority))
                self.driver.invoke("click", selector="保存")
            water = "火种：净水基础；类别：生存基础；优先级：5；状态：沉睡"
            self.driver.invoke("click", selector="开始重建", scope=water)
            restoring = (
                "火种：净水基础；类别：生存基础；优先级：5；状态：重建中"
            )
            self.driver.invoke("click", selector="完成复原", scope=restoring)
            return
        if kind == "back":
            self.driver.invoke("back")
            return
        self.driver.invoke(
            kind,
            selector=action.get("selector"),
            scope=action.get("scope"),
            value=action.get("value"),
        )

    def _capture_screen(
        self, scale_tag: str, screen: dict[str, Any], density: float
    ) -> dict[str, Any]:
        screen_id = screen["id"]
        root = self.artifacts / "accessibility" / scale_tag / screen_id
        hierarchy_path = root / "hierarchy.json"
        self.driver.dump(f"a08-{scale_tag}-{screen_id}-hierarchy.json", hierarchy_path)
        hierarchy = _load(hierarchy_path)
        expected = screen["expected"]
        if not hierarchy_matches(hierarchy, expected, []):
            raise OracleError(f"A08 hierarchy mismatch: {scale_tag}/{screen_id}")
        accessibility = analyze_accessibility_hierarchy(hierarchy, density=density)
        if accessibility["result"] != "pass":
            raise OracleError(
                f"A08 touch-target/name failure: {scale_tag}/{screen_id}"
            )

        screenshot_path = root / "screen.png"
        self.driver.screenshot(
            f"a08-{scale_tag}-{screen_id}.png", screenshot_path
        )
        image = decode_png(screenshot_path.read_bytes())
        contrast_documents = []
        contrast_index = []
        for index, node in enumerate(hierarchy.get("nodes", [])):
            text = node.get("text") or ""
            if (
                not text
                or node.get("text_utf8_valid") is not True
                or node.get("visible_to_user") is not True
            ):
                continue
            box_name = f"n{index:04d}-boxes.json"
            box_path = root / "character-boxes" / box_name
            self.driver.character_boxes(
                node_path=node["path"],
                evidence_name=(
                    f"a08-{scale_tag}-{screen_id}-n{index:04d}-boxes.json"
                ),
                destination=box_path,
            )
            document = _load(box_path)
            document["node_path"] = node["path"]
            contrast_documents.append(document)
            contrast_index.append(
                {
                    "path": node["path"],
                    "text": text,
                    "character_boxes_path": box_path.as_posix(),
                }
            )
        expected_text_count = sum(
            1
            for node in hierarchy.get("nodes", [])
            if node.get("text")
            and node.get("text_utf8_valid") is True
            and node.get("visible_to_user") is True
        )
        if len(contrast_documents) != expected_text_count:
            raise OracleError("A08 visible text contrast coverage is incomplete")
        contrast = measure_screen_text_contrast(image, contrast_documents)
        if contrast["result"] != "pass":
            raise OracleError(f"A08 contrast failure: {scale_tag}/{screen_id}")
        contrast_path = root / "contrast-report.json"
        write_new_or_replace(contrast_path, canonical_json_bytes(contrast))
        mask_document = {
            "schema_version": 1,
            "screen_id": screen_id,
            "font_scale": float(scale_tag.replace("x", ".")),
            "characters": [
                {
                    "node_path": node["node_path"],
                    "code_point_index": character["code_point_index"],
                    "code_point": character["code_point"],
                    "pixel_box": character["pixel_box"],
                    "selected_actual_pixels": character["measurement"][
                        "selected_actual_pixels"
                    ],
                }
                for node in contrast["nodes"]
                for character in node["characters"]
            ],
            "result": "pass",
        }
        write_new_or_replace(
            self.artifacts
            / "contrast-masks"
            / f"{scale_tag}-{screen_id}.json",
            canonical_json_bytes(mask_document),
        )

        first_name = expected[0].split("|", 1)[0]
        focus_name = f"a08-{scale_tag}-{screen_id}-focus.json"
        receipt = self.driver.invoke(
            "focus_trace",
            selector=first_name,
            evidence_name=focus_name,
            extra={"count": len(expected) + 1, "interval_ms": 700},
        )
        focus_path = root / "talkback-focus.json"
        copied = self.driver.copy_private_evidence(focus_name, focus_path)
        if copied["sha256"] != receipt.get("evidence_sha256"):
            raise OracleError("A08 focus trace changed before host copy")
        focus = verify_focus_signature_cycle(_load(focus_path), expected)
        return {
            "screen_id": screen_id,
            "font_scale": float(scale_tag.replace("x", ".")),
            "hierarchy_path": hierarchy_path.as_posix(),
            "screenshot_path": screenshot_path.as_posix(),
            "accessibility": accessibility,
            "talkback": focus,
            "visible_text_node_count": expected_text_count,
            "text_character_boxes": contrast_index,
            "contrast_report_path": contrast_path.as_posix(),
            "contrast": contrast,
            "result": "pass",
        }

    def run(self) -> dict[str, Any]:
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.device.set_talkback(True)
        talkback = self.device.validate_talkback()
        density = self.device.display_density()
        reports = []
        try:
            for scale in self.contract["font_scales"]:
                scale_tag = "1x0" if scale == 1.0 else "2x0"
                self.device.set_font_scale(scale)
                self.device.clear_data()
                self.device.force_stop()
                self.device.launch()
                for screen in self.contract["screens"]:
                    for action in screen.get("enter", []):
                        self._perform(action)
                    reports.append(
                        self._capture_screen(scale_tag, screen, density)
                    )
                    if scale == 2.0 and screen["id"] == "onboarding":
                        source = (
                            self.artifacts
                            / "accessibility"
                            / scale_tag
                            / screen["id"]
                            / "screen.png"
                        )
                        write_new_or_replace(
                            self.artifacts
                            / "screenshots"
                            / "06_font_scale_200.png",
                            source.read_bytes(),
                        )
                    for action in screen.get("leave", []):
                        self._perform(action)
                if scale == 2.0:
                    self.device.clear_data()
                    self.device.force_stop()
                    self.device.launch()
                    rerun = WorkflowRunner(self.device, self.output / "font-200-a06").run(
                        FLOW_CONFIG
                    )
                    if rerun.get("result") != "pass" or not rerun.get(
                        "complete_frozen_run"
                    ):
                        raise OracleError("A08 200-percent complete A06 rerun failed")
        finally:
            self.device.set_talkback(False)
        if len(reports) != 20:
            raise OracleError("A08 requires exactly ten screens at two font scales")
        all_box_bindings = {
            "schema_version": 1,
            "screen_count": len(reports),
            "screens": [
                {
                    "screen_id": item["screen_id"],
                    "font_scale": item["font_scale"],
                    "visible_text_node_count": item["visible_text_node_count"],
                    "nodes": item["text_character_boxes"],
                }
                for item in reports
            ],
            "result": "pass",
        }
        all_contrast = {
            "schema_version": 1,
            "screen_count": len(reports),
            "screens": [
                {
                    "screen_id": item["screen_id"],
                    "font_scale": item["font_scale"],
                    "report_path": item["contrast_report_path"],
                    "minimum_contrast_ratio": item["contrast"][
                        "minimum_contrast_ratio"
                    ],
                }
                for item in reports
            ],
            "result": "pass",
        }
        all_focus = {
            "schema_version": 1,
            "screen_count": len(reports),
            "screens": [
                {
                    "screen_id": item["screen_id"],
                    "font_scale": item["font_scale"],
                    "verification": item["talkback"],
                }
                for item in reports
            ],
            "result": "pass",
        }
        for name, document in (
            ("text-character-boxes.json", all_box_bindings),
            ("contrast-report.json", all_contrast),
            ("talkback-focus-trace.json", all_focus),
        ):
            write_new_or_replace(
                self.artifacts / name, canonical_json_bytes(document)
            )
        result = {
            "schema_version": 1,
            "case_id": "A08",
            "talkback": talkback,
            "display_density": density,
            "screen_count": len(reports),
            "screens": reports,
            "font_scale_200_complete_a06": True,
            "result": "pass",
        }
        write_new_or_replace(
            self.artifacts / "accessibility-report.json",
            canonical_json_bytes(result),
        )
        return result


_BOUNDS = re.compile(r"^\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]$")


def _bounds(value: str) -> tuple[int, int, int, int]:
    match = _BOUNDS.fullmatch(value)
    if not match:
        raise OracleError(f"invalid pixel bounds: {value!r}")
    return tuple(map(int, match.groups()))


class NormalMotionRunner:
    """Capture and deterministically measure the complete normal-motion A09 lane."""

    def __init__(self, device: AndroidDevice, output: Path):
        self.device = device
        self.output = output
        self.artifacts = output / "artifacts"
        self.driver = SemanticDriver(device, self.artifacts)

    def run_machine(self) -> dict[str, Any]:
        self.device.clear_data()
        self.device.set_font_scale(1.0)
        self.device.set_animation_scales(1.0)
        self.device.force_stop()
        self.device.launch()
        root = self.artifacts / "motion-frames"
        capture = self.driver.capture_normal_motion(
            trace_name="normal-motion.json",
            destination=root,
        )
        trace = capture["trace"]
        frames_meta = trace.get("frames")
        if not isinstance(frames_meta, list) or len(frames_meta) != 60:
            raise OracleError("normal motion requires exactly 60 captured frames")
        geometries = {
            (
                frame.get("width"),
                frame.get("height"),
                frame.get("application_content_bounds"),
            )
            for frame in frames_meta
        }
        if len(geometries) != 1:
            raise OracleError("normal-motion geometry changed")
        starts = [frame.get("capture_start_uptime_ms") for frame in frames_meta]
        if any(not isinstance(value, int) for value in starts):
            raise OracleError("normal-motion frame lacks capture-start timestamp")
        frame_images = [
            decode_png((root / frame["evidence_name"]).read_bytes())
            for frame in frames_meta
        ]
        hierarchy = trace.get("before_hierarchy")
        if not isinstance(hierarchy, dict):
            raise OracleError("normal motion lacks bound initial hierarchy")
        hero_nodes = _matches(
            hierarchy, "莉莉丝，闭着双眼，白发向两侧展开"
        )
        if len(hero_nodes) != 1:
            raise OracleError("normal motion hero node is not unique")
        content_bounds = _bounds(frames_meta[0]["application_content_bounds"])
        hero_screen = _bounds(hero_nodes[0]["bounds"])
        left = max(content_bounds[0], hero_screen[0] - 8) - content_bounds[0]
        top = max(content_bounds[1], hero_screen[1] - 8) - content_bounds[1]
        right = min(content_bounds[2], hero_screen[2] + 8) - content_bounds[0]
        bottom = min(content_bounds[3], hero_screen[3] + 8) - content_bounds[1]
        hero_roi = (left, top, right, bottom)
        measurement = measure_normal_motion(frame_images, starts, hero_roi)
        for index, silhouette in enumerate(
            measurement["per_frame_silhouettes"]
        ):
            write_new_or_replace(
                self.artifacts
                / "motion-masks"
                / f"normal-f{index:02d}.json",
                canonical_json_bytes(
                    {
                        "schema_version": 1,
                        "frame_index": index,
                        "hero_roi": list(hero_roi),
                        "mask_points": silhouette["mask_points"],
                        "mask_pixel_count": silhouette["mask_pixel_count"],
                        "result": "pass",
                    }
                ),
            )
        frame_bindings = [
            {
                "request_sequence": frame["request_sequence"],
                "capture_start_uptime_ms": frame["capture_start_uptime_ms"],
                "capture_complete_uptime_ms": frame["capture_complete_uptime_ms"],
                "pixel_buffer_sha256": frame["pixel_buffer_sha256"],
                "png_sha256": frame["sha256"],
            }
            for frame in frames_meta
        ]
        frame_set_sha256 = hashlib.sha256(
            canonical_json_bytes(frame_bindings)
        ).hexdigest()
        result = {
            "schema_version": 1,
            "case_id": "A09-normal-motion-machine",
            "status": "ready_for_visual_review",
            "frame_set_sha256": frame_set_sha256,
            "trace_file": capture["trace_file"],
            "frame_bindings": frame_bindings,
            "measurement": measurement,
            "result": "pass",
        }
        write_new_or_replace(
            self.artifacts / "motion-report.json",
            canonical_json_bytes(result),
        )
        return result


def finalize_a09(root: Path, visual_review_path: Path) -> dict[str, Any]:
    artifacts = root / "artifacts"
    if visual_review_path.is_symlink():
        raise OracleError("A09 visual review cannot be a symbolic link")
    visual_review_path = visual_review_path.resolve()
    if not visual_review_path.is_file():
        raise OracleError("A09 visual review must be a regular evidence file")
    machine_path = artifacts / "motion-report.json"
    reduced_path = artifacts / "reduced-motion-report.json"
    machine = _load(machine_path)
    reduced = _load(reduced_path)
    review = _load(visual_review_path)
    required_review = {
        "reviewer_identity",
        "review_context",
        "reviewed_frame_set_sha256",
        "reviewed_reduced_frame_set_sha256",
        "restrained_dark_purple_style",
        "character_contract",
        "closed_eyes_all_frames",
        "reduced_closed_eyes_all_frames",
        "per_frame_closed_eyes",
        "fresh_context",
        "read_only_review",
        "final_verdict",
    }
    if not required_review.issubset(review):
        raise OracleError("A09 visual review lacks frozen required fields")
    if (
        not isinstance(review["reviewer_identity"], str)
        or not review["reviewer_identity"]
        or not isinstance(review["review_context"], str)
        or not review["review_context"]
        or review["reviewed_frame_set_sha256"] != machine["frame_set_sha256"]
        or review["reviewed_reduced_frame_set_sha256"]
        != reduced.get("frame_set_sha256")
        or review["restrained_dark_purple_style"] != "PASS"
        or review["character_contract"] != "PASS"
        or review["closed_eyes_all_frames"] is not True
        or review["reduced_closed_eyes_all_frames"] is not True
        or review["fresh_context"] is not True
        or review["read_only_review"] is not True
        or review["per_frame_closed_eyes"] != [
            {"request_sequence": index, "closed_eyes": True}
            for index in range(60)
        ]
        or reduced.get("retained_frame_count") != 221
        or review["final_verdict"] != "PASS"
    ):
        raise OracleError("A09 visual review is not exact zero-blocker PASS")
    if machine.get("result") != "pass" or reduced.get("result") != "pass":
        raise OracleError("A09 machine lanes are not both PASS")
    try:
        review_relative = visual_review_path.relative_to(artifacts.resolve())
    except ValueError as error:
        raise OracleError("A09 visual review must be below artifacts/") from error
    result = {
        "schema_version": 1,
        "case_id": "A09",
        "normal_motion_report": {
            "path": "artifacts/motion-report.json",
            "sha256": sha256_file(machine_path),
        },
        "reduced_motion_report": {
            "path": "artifacts/reduced-motion-report.json",
            "sha256": sha256_file(reduced_path),
        },
        "visual_review": {
            "path": "artifacts/" + review_relative.as_posix(),
            "sha256": sha256_file(visual_review_path),
        },
        "result": "pass",
    }
    write_new_or_replace(
        artifacts / "a09-result.json", canonical_json_bytes(result)
    )
    return result
