import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Optional

from .constants import DRIVER_PACKAGE, INSTRUMENTATION
from .device import AndroidDevice
from .util import OracleError, sha256_file, write_new_or_replace
from .png import decode_png


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8", errors="strict")).decode("ascii")


def _pixel_buffer_sha256(path: Path) -> str:
    image = decode_png(path.read_bytes())
    digest = hashlib.sha256()
    for row in image.pixels:
        for red, green, blue in row:
            digest.update(bytes((255, red, green, blue)))
    return digest.hexdigest()


class SemanticDriver:
    """Host wrapper around the frozen source-independent instrumentation."""

    def __init__(self, device: AndroidDevice, evidence_root: Path):
        self.device = device
        self.evidence_root = evidence_root

    def invoke(
        self,
        action: str,
        *,
        selector: Optional[str] = None,
        selector_type: str = "any",
        scope: Optional[str] = None,
        scope_type: str = "any",
        value: Optional[str] = None,
        utf16_hex: Optional[str] = None,
        evidence_name: Optional[str] = None,
        extra: Optional[dict[str, object]] = None,
    ) -> dict[str, str]:
        args: list[object] = [
            "am",
            "instrument",
            "-w",
            "-r",
            "-e",
            "action",
            action,
        ]
        if selector is not None:
            args.extend(["-e", "selector_base64", _b64(selector)])
            args.extend(["-e", "selector_type", selector_type])
        if scope is not None:
            args.extend(["-e", "scope_selector_base64", _b64(scope)])
            args.extend(["-e", "scope_selector_type", scope_type])
        if value is not None:
            args.extend(["-e", "value_base64", _b64(value)])
        if utf16_hex is not None:
            if (
                not utf16_hex
                or len(utf16_hex) % 4
                or any(char not in "0123456789abcdef" for char in utf16_hex)
            ):
                raise OracleError("UTF-16 transport requires lowercase 4-digit code units")
            args.extend(["-e", "value_utf16_hex", utf16_hex])
        if evidence_name is not None:
            args.extend(["-e", "evidence_name", evidence_name])
        for key, item in sorted((extra or {}).items()):
            if key in {"x", "y", "coordinate", "coordinates", "global_ordinal"}:
                raise OracleError("coordinates/ordinals are forbidden as oracle inputs")
            args.extend(["-e", key, str(item)])
        args.append(INSTRUMENTATION)
        result = self.device.shell(*args, timeout=90.0, check=False)
        parsed: dict[str, str] = {}
        combined = (result.stdout + b"\n" + result.stderr).decode(
            "utf-8", errors="replace"
        )
        for line in combined.splitlines():
            match = re.match(r"INSTRUMENTATION_(?:RESULT|STATUS): ([^=]+)=(.*)", line)
            if match:
                parsed[match.group(1)] = match.group(2)
        status = parsed.get("status")
        if result.returncode != 0 or status != "pass":
            raise OracleError(
                f"instrumentation {action} failed: "
                f"{parsed.get('error_type', '')}: {parsed.get('error', combined[-2000:])}"
            )
        return parsed

    def copy_private_evidence(self, name: str, destination: Path) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", name):
            raise OracleError("unsafe driver evidence name")
        remote = f"files/t01m-oracle/{name}"
        raw = self.device.adb_cmd(
            "exec-out", "run-as", DRIVER_PACKAGE, "cat", remote
        ).stdout
        write_new_or_replace(destination, raw)
        return {
            "path": destination.as_posix(),
            "bytes": len(raw),
            "sha256": sha256_file(destination),
        }

    def dump(self, name: str, destination: Path) -> dict[str, Any]:
        receipt = self.invoke("dump", evidence_name=name)
        copied = self.copy_private_evidence(name, destination)
        if copied["sha256"] != receipt.get("evidence_sha256"):
            raise OracleError("hierarchy changed between driver write and host copy")
        return copied

    def screenshot(self, name: str, destination: Path) -> dict[str, Any]:
        receipt = self.invoke("screenshot", evidence_name=name)
        copied = self.copy_private_evidence(name, destination)
        if copied["sha256"] != receipt.get("evidence_sha256"):
            raise OracleError("screenshot changed between driver write and host copy")
        return copied

    def capture_transition(
        self,
        *,
        transition_id: str,
        action_kind: str,
        trace_name: str,
        destination: Path,
        selector: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> dict[str, Any]:
        receipt = self.invoke(
            "transition_capture",
            selector=selector,
            scope=scope,
            evidence_name=trace_name,
            extra={
                "transition_id": transition_id,
                "transition_action": action_kind,
            },
        )
        trace_path = destination / trace_name
        copied_trace = self.copy_private_evidence(trace_name, trace_path)
        with trace_path.open("r", encoding="utf-8") as source:
            trace = json.load(source)
        frames = []
        for frame in trace.get("frames", []):
            name = frame.get("evidence_name")
            if not isinstance(name, str):
                raise OracleError("transition trace frame lacks evidence_name")
            copied = self.copy_private_evidence(name, destination / name)
            if copied["sha256"] != frame.get("sha256"):
                raise OracleError("transition frame changed before host copy")
            if _pixel_buffer_sha256(destination / name) != frame.get(
                "pixel_buffer_sha256"
            ):
                raise OracleError("transition immutable pixel-buffer digest mismatch")
            frames.append(copied)
        return {
            "receipt": receipt,
            "trace": trace,
            "trace_file": copied_trace,
            "frames": frames,
        }

    def character_boxes(
        self,
        *,
        selector: Optional[str] = None,
        node_path: Optional[str] = None,
        scope: Optional[str] = None,
        evidence_name: str,
        destination: Path,
    ) -> dict[str, Any]:
        if (selector is None) == (node_path is None):
            raise OracleError("character boxes require exactly one selector or node path")
        receipt = self.invoke(
            "character_boxes",
            selector=selector,
            scope=scope,
            evidence_name=evidence_name,
            extra={"node_path": node_path} if node_path is not None else None,
        )
        copied = self.copy_private_evidence(evidence_name, destination)
        if copied["sha256"] != receipt.get("evidence_sha256"):
            raise OracleError("character-box evidence changed before host copy")
        return copied

    def capture_idle(
        self,
        *,
        state_id: str,
        trace_name: str,
        destination: Path,
    ) -> dict[str, Any]:
        receipt = self.invoke(
            "idle_capture",
            evidence_name=trace_name,
            extra={"state_id": state_id},
        )
        trace_path = destination / trace_name
        copied_trace = self.copy_private_evidence(trace_name, trace_path)
        with trace_path.open("r", encoding="utf-8") as source:
            trace = json.load(source)
        frames = []
        for frame in trace.get("frames", []):
            name = frame.get("evidence_name")
            if not isinstance(name, str):
                raise OracleError("idle trace frame lacks evidence_name")
            copied = self.copy_private_evidence(name, destination / name)
            if copied["sha256"] != frame.get("sha256"):
                raise OracleError("idle frame changed before host copy")
            if _pixel_buffer_sha256(destination / name) != frame.get(
                "pixel_buffer_sha256"
            ):
                raise OracleError("idle immutable pixel-buffer digest mismatch")
            frames.append(copied)
        return {
            "receipt": receipt,
            "trace": trace,
            "trace_file": copied_trace,
            "frames": frames,
        }

    def capture_normal_motion(
        self, *, trace_name: str, destination: Path
    ) -> dict[str, Any]:
        receipt = self.invoke(
            "normal_motion_capture",
            evidence_name=trace_name,
        )
        trace_path = destination / trace_name
        copied_trace = self.copy_private_evidence(trace_name, trace_path)
        with trace_path.open("r", encoding="utf-8") as source:
            trace = json.load(source)
        frames = []
        for frame in trace.get("frames", []):
            name = frame.get("evidence_name")
            if not isinstance(name, str):
                raise OracleError("normal-motion frame lacks evidence_name")
            path = destination / name
            copied = self.copy_private_evidence(name, path)
            if copied["sha256"] != frame.get("sha256"):
                raise OracleError("normal-motion frame changed before host copy")
            if _pixel_buffer_sha256(path) != frame.get("pixel_buffer_sha256"):
                raise OracleError("normal-motion immutable pixel digest mismatch")
            frames.append(copied)
        return {
            "receipt": receipt,
            "trace": trace,
            "trace_file": copied_trace,
            "frames": frames,
        }
