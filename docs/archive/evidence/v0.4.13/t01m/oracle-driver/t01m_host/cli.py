import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import load_accessibility_contract, load_flow
from .constants import FLOW_CONFIG
from .constants import REDUCED_MOTION_CONFIG
from .device import (
    AndroidDevice,
    validate_oracle_lock,
    validate_target_apk,
    validate_toolchain,
)
from .evidence import (
    build_and_validate_closure,
    build_evidence_leaves,
    build_evidence_manifest,
    validate_control_graph,
    verify_evidence_leaves,
)
from .util import (
    OracleError,
    canonical_json_bytes,
    sha256_file,
    write_new_or_replace,
)
from .workflow import WorkflowRunner
from .measure import measure_text_node_contrast
from .png import decode_png
from .runtime import (
    analyze_accessibility_hierarchy,
    capture_runtime_state,
    compare_runtime_states,
)
from .event_binding import (
    ReducedMotionRunner,
    load_reduced_motion_contract,
    validate_transition_capture,
)
from .case_runners import (
    AccessibilityRunner,
    NormalMotionRunner,
    PersistenceRunner,
    finalize_a09,
)
from .apk_security import analyze_apk_security
from .static_cases import verify_static_cases


def _json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise OracleError(f"expected JSON object: {path}")
    return value


def _emit(value: Any) -> None:
    sys.stdout.buffer.write(canonical_json_bytes(value))


def _snapshot(device: AndroidDevice, destination: Path) -> dict[str, Any]:
    document = {
        "schema_version": 1,
        "root": "/storage/emulated/0",
        "files": device.snapshot_shared_storage(),
    }
    write_new_or_replace(destination, canonical_json_bytes(document))
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="t01m-host-oracle",
        description="Frozen application-source-independent T01M Android host oracle",
    )
    parser.add_argument(
        "--validate-config",
        action="store_true",
        help="validate only frozen host configuration; never requires or executes a target APK",
    )
    subparsers = parser.add_subparsers(dest="command")

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--serial", required=True)
    preflight.add_argument("--apk", required=True, type=Path)

    static = subparsers.add_parser("verify-a01-a04")
    static.add_argument("--repository", required=True, type=Path)
    static.add_argument("--accepted-commit", required=True)
    static.add_argument("--assignment-ledger", required=True, type=Path)
    static.add_argument("--build-receipt", required=True, type=Path)
    static.add_argument("--rebuild-receipt-a", required=True, type=Path)
    static.add_argument("--rebuild-receipt-b", required=True, type=Path)
    static.add_argument("--output", required=True, type=Path)

    security = subparsers.add_parser("run-a05")
    security.add_argument("--apk", required=True, type=Path)
    security.add_argument("--output", required=True, type=Path)

    run_a06 = subparsers.add_parser("run-a06")
    run_a06.add_argument("--serial", required=True)
    run_a06.add_argument("--apk", required=True, type=Path)
    run_a06.add_argument("--output", required=True, type=Path)

    reduced = subparsers.add_parser("run-reduced-motion")
    reduced.add_argument("--serial", required=True)
    reduced.add_argument("--apk", required=True, type=Path)
    reduced.add_argument("--output", required=True, type=Path)

    a07 = subparsers.add_parser("run-a07")
    a07.add_argument("--serial", required=True)
    a07.add_argument("--apk", required=True, type=Path)
    a07.add_argument("--output", required=True, type=Path)
    a07.add_argument("--observability-client", required=True, type=Path)

    a08 = subparsers.add_parser("run-a08")
    a08.add_argument("--serial", required=True)
    a08.add_argument("--apk", required=True, type=Path)
    a08.add_argument("--output", required=True, type=Path)

    normal_motion = subparsers.add_parser("capture-normal-motion")
    normal_motion.add_argument("--serial", required=True)
    normal_motion.add_argument("--apk", required=True, type=Path)
    normal_motion.add_argument("--output", required=True, type=Path)

    finalize_motion = subparsers.add_parser("finalize-a09")
    finalize_motion.add_argument("--root", required=True, type=Path)
    finalize_motion.add_argument("--visual-review", required=True, type=Path)

    transition = subparsers.add_parser("validate-transition")
    transition.add_argument("--trace", required=True, type=Path)

    contrast = subparsers.add_parser("analyze-contrast")
    contrast.add_argument("--screenshot", required=True, type=Path)
    contrast.add_argument("--character-boxes", required=True, type=Path)
    contrast.add_argument("--output", required=True, type=Path)

    control = subparsers.add_parser("device-control")
    control.add_argument("--serial", required=True)
    control.add_argument(
        "operation",
        choices=[
            "clear-data",
            "force-stop",
            "launch",
            "offline",
            "portrait",
            "landscape",
            "font-100",
            "font-200",
            "animations-on",
            "animations-off",
            "talkback-on",
            "talkback-off",
        ],
    )

    snapshot = subparsers.add_parser("snapshot-shared-storage")
    snapshot.add_argument("--serial", required=True)
    snapshot.add_argument("--output", required=True, type=Path)

    runtime_capture = subparsers.add_parser("capture-runtime-state")
    runtime_capture.add_argument("--serial", required=True)
    runtime_capture.add_argument("--output", required=True, type=Path)

    runtime_compare = subparsers.add_parser("compare-runtime-states")
    runtime_compare.add_argument("--before", required=True, type=Path)
    runtime_compare.add_argument("--after", required=True, type=Path)
    runtime_compare.add_argument("--socket-interval", type=Path)

    accessibility = subparsers.add_parser("analyze-accessibility")
    accessibility.add_argument("--hierarchy", required=True, type=Path)
    accessibility.add_argument("--density", required=True, type=float)
    accessibility.add_argument("--output", required=True, type=Path)

    leaves = subparsers.add_parser("build-leaves")
    leaves.add_argument("--root", required=True, type=Path)
    leaves.add_argument("--artifact-index", required=True, type=Path)

    manifest = subparsers.add_parser("build-manifest")
    manifest.add_argument("--root", required=True, type=Path)
    manifest.add_argument("--metadata", required=True, type=Path)

    graph = subparsers.add_parser("validate-graph")
    graph.add_argument("--root", required=True, type=Path)

    closure = subparsers.add_parser("build-closure")
    closure.add_argument("--root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.validate_config:
            if args.command is not None:
                raise OracleError("--validate-config cannot be combined with a command")
            flow = load_flow(FLOW_CONFIG)
            reduced_contract = load_reduced_motion_contract(
                REDUCED_MOTION_CONFIG
            )
            accessibility_contract = load_accessibility_contract()
            tools = validate_toolchain()
            lock = validate_oracle_lock()
            _emit(
                {
                    "schema_version": 1,
                    "mode": "validate_config_only",
                    "flow_step_count": len(flow["steps"]),
                    "reduced_motion_transition_count": len(
                        reduced_contract["transition_targets"]
                    ),
                    "accessibility_screen_count": len(
                        accessibility_contract["screens"]
                    ),
                    "toolchain": tools,
                    "oracle_lock": lock,
                    "target_apk_accessed": False,
                    "runtime_pass_claimed": False,
                    "result": "pass",
                }
            )
            return 0
        if args.command is None:
            parser.error("choose --validate-config or a command")
        if args.command == "preflight":
            tools = validate_toolchain()
            lock = validate_oracle_lock()
            device = AndroidDevice(args.serial)
            _emit(
                {
                    "toolchain": tools,
                    "oracle_lock": lock,
                    "device": device.validate_identity(),
                    "apk": validate_target_apk(args.apk),
                }
            )
        elif args.command == "verify-a01-a04":
            validate_oracle_lock()
            _emit(
                verify_static_cases(
                    repository=args.repository,
                    accepted_commit=args.accepted_commit,
                    assignment_ledger=args.assignment_ledger,
                    build_receipt=args.build_receipt,
                    rebuild_receipt_a=args.rebuild_receipt_a,
                    rebuild_receipt_b=args.rebuild_receipt_b,
                    output=args.output,
                )
            )
        elif args.command == "run-a05":
            validate_toolchain()
            validate_oracle_lock()
            _emit(analyze_apk_security(args.apk, args.output))
        elif args.command == "run-a06":
            validate_toolchain()
            validate_oracle_lock()
            device = AndroidDevice(args.serial)
            identity = device.validate_identity()
            package = validate_target_apk(args.apk)
            device.set_offline()
            before1 = _snapshot(
                device, args.output / "artifacts/shared-storage-before-1.json"
            )
            before2 = _snapshot(
                device, args.output / "artifacts/shared-storage-before-2.json"
            )
            if before1["files"] != before2["files"]:
                raise OracleError("shared storage was not quiescent before installation")
            device.install_driver()
            device.install_target(args.apk)
            device.clear_data()
            device.set_font_scale(1.0)
            device.set_animation_scales(1.0)
            device.launch()
            trace = WorkflowRunner(device, args.output).run(FLOW_CONFIG)
            if trace.get("result") != "pass" or not trace.get("complete_frozen_run"):
                raise OracleError("A06 did not execute the complete frozen flow")
            after1 = _snapshot(
                device, args.output / "artifacts/shared-storage-after-1.json"
            )
            after2 = _snapshot(
                device, args.output / "artifacts/shared-storage-after-2.json"
            )
            if after1["files"] != after2["files"]:
                raise OracleError("shared storage was not quiescent after A06")
            if before1["files"] != after1["files"]:
                raise OracleError("target modified shared storage")
            write_new_or_replace(
                args.output / "artifacts/shared-storage-diff.json",
                canonical_json_bytes(
                    {
                        "schema_version": 1,
                        "before_sha256": sha256_file(
                            args.output
                            / "artifacts/shared-storage-before-1.json"
                        ),
                        "after_sha256": sha256_file(
                            args.output
                            / "artifacts/shared-storage-after-1.json"
                        ),
                        "added": [],
                        "removed": [],
                        "changed": [],
                        "result": "pass",
                    }
                ),
            )
            result = {
                "schema_version": 1,
                "case_id": "A06",
                "device": identity,
                "apk": package,
                "network_disabled_before_install": True,
                "font_scale": 1.0,
                "animation_scale": 1.0,
                "shared_storage_diff": [],
                "trace": trace,
                "result": "pass",
            }
            write_new_or_replace(
                args.output / "artifacts/oracle-result.json",
                canonical_json_bytes(result),
            )
            _emit(result)
        elif args.command == "device-control":
            device = AndroidDevice(args.serial)
            operations = {
                "clear-data": device.clear_data,
                "force-stop": device.force_stop,
                "launch": device.launch,
                "offline": device.set_offline,
                "portrait": lambda: device.rotate("portrait"),
                "landscape": lambda: device.rotate("landscape"),
                "font-100": lambda: device.set_font_scale(1.0),
                "font-200": lambda: device.set_font_scale(2.0),
                "animations-on": lambda: device.set_animation_scales(1.0),
                "animations-off": lambda: device.set_animation_scales(0.0),
                "talkback-on": lambda: (device.set_talkback(True), device.validate_talkback()),
                "talkback-off": lambda: device.set_talkback(False),
            }
            operations[args.operation]()
            _emit({"operation": args.operation, "result": "pass"})
        elif args.command == "run-reduced-motion":
            validate_toolchain()
            validate_oracle_lock()
            device = AndroidDevice(args.serial)
            identity = device.validate_identity()
            package = validate_target_apk(args.apk)
            device.set_offline()
            device.install_driver()
            device.install_target(args.apk)
            report = ReducedMotionRunner(device, args.output).run()
            report["device"] = identity
            report["apk"] = package
            _emit(report)
        elif args.command == "run-a07":
            validate_toolchain()
            validate_oracle_lock()
            device = AndroidDevice(args.serial)
            identity = device.validate_identity()
            package = validate_target_apk(args.apk)
            device.set_offline()
            device.install_driver()
            device.install_target(args.apk)
            report = PersistenceRunner(device, args.output).run(
                observability_client=args.observability_client,
            )
            report["device"] = identity
            report["apk"] = package
            _emit(report)
        elif args.command == "run-a08":
            validate_toolchain()
            validate_oracle_lock()
            device = AndroidDevice(args.serial)
            identity = device.validate_identity()
            package = validate_target_apk(args.apk)
            device.set_offline()
            device.install_driver()
            device.install_target(args.apk)
            report = AccessibilityRunner(device, args.output).run()
            report["device"] = identity
            report["apk"] = package
            _emit(report)
        elif args.command == "capture-normal-motion":
            validate_toolchain()
            validate_oracle_lock()
            device = AndroidDevice(args.serial)
            identity = device.validate_identity()
            package = validate_target_apk(args.apk)
            device.set_offline()
            device.install_driver()
            device.install_target(args.apk)
            report = NormalMotionRunner(device, args.output).run_machine()
            report["device"] = identity
            report["apk"] = package
            _emit(report)
        elif args.command == "finalize-a09":
            validate_oracle_lock()
            _emit(
                finalize_a09(
                    args.root.resolve(), args.visual_review.resolve()
                )
            )
        elif args.command == "validate-transition":
            _emit(validate_transition_capture(_json_file(args.trace)))
        elif args.command == "analyze-contrast":
            image = decode_png(args.screenshot.read_bytes())
            report = measure_text_node_contrast(
                image, _json_file(args.character_boxes)
            )
            write_new_or_replace(args.output, canonical_json_bytes(report))
            _emit(report)
        elif args.command == "snapshot-shared-storage":
            _emit(_snapshot(AndroidDevice(args.serial), args.output))
        elif args.command == "capture-runtime-state":
            _emit(
                capture_runtime_state(
                    AndroidDevice(args.serial),
                    args.output,
                )
            )
        elif args.command == "compare-runtime-states":
            _emit(
                compare_runtime_states(
                    args.before,
                    args.after,
                    socket_interval_path=args.socket_interval,
                )
            )
        elif args.command == "analyze-accessibility":
            report = analyze_accessibility_hierarchy(
                _json_file(args.hierarchy), density=args.density
            )
            write_new_or_replace(args.output, canonical_json_bytes(report))
            _emit(report)
        elif args.command == "build-leaves":
            validate_oracle_lock()
            root = args.root.resolve()
            result = build_evidence_leaves(
                root / "artifacts",
                root / "evidence-leaves.json",
                _json_file(args.artifact_index),
            )
            _emit(result)
        elif args.command == "build-manifest":
            validate_oracle_lock()
            root = args.root.resolve()
            result = build_evidence_manifest(
                root, _json_file(args.metadata), root / "evidence-manifest.json"
            )
            _emit(result)
        elif args.command == "validate-graph":
            validate_oracle_lock()
            _emit(validate_control_graph(args.root.resolve()))
        elif args.command == "build-closure":
            validate_oracle_lock()
            root = args.root.resolve()
            _emit(build_and_validate_closure(root, root / "closure-envelope.json"))
        return 0
    except (OracleError, OSError, ValueError, json.JSONDecodeError) as error:
        _emit({"result": "fail", "error_type": type(error).__name__, "error": str(error)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
