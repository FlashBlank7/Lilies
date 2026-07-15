#!/usr/bin/env python3
"""Reject v0.4.3 browser closure without complete, inspectable UI evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def versioned_evidence_path(filename: str) -> Path:
    active = ROOT / "docs/workingon" / filename
    if active.exists():
        return active
    return ROOT / "docs/workingon-archives/v0.4.3" / filename


DEFAULT_JOURNEY = versioned_evidence_path("v0.4.3_browser_journey.json")
DEFAULT_EVIDENCE = versioned_evidence_path("v0.4.3_browser_verification.json")


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return payload


def png_dimensions(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError("not a PNG with an IHDR header")
    return struct.unpack(">II", header[16:24])


def _is_inside(path: Path, directory: Path) -> bool:
    return path == directory or directory in path.parents


def validate_browser_closure(
    *,
    root: Path,
    journey: dict[str, Any],
    evidence: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    if journey.get("stage") != "v0.4.3" or journey.get("task_id") != "V04-03-T01F":
        errors.append("journey must identify v0.4.3/V04-03-T01F")
    if evidence.get("stage") != journey.get("stage") or evidence.get("task_id") != journey.get(
        "task_id"
    ):
        errors.append("evidence stage/task does not match the journey")
    if evidence.get("status") != "passed":
        errors.append("browser evidence status must be passed")

    runtime = evidence.get("runtime_health")
    if not isinstance(runtime, dict) or runtime.get("status") != "ok" or not runtime.get(
        "current_code_ready"
    ):
        errors.append("current backend runtime health is not proven")
    frontend = evidence.get("frontend_runtime")
    if not isinstance(frontend, dict) or frontend.get("status") != "ready":
        errors.append("current frontend runtime is not proven ready")
    elif frontend.get("http_status") != 200 or frontend.get("proxy_fixture_status") != 200:
        errors.append("frontend page and API proxy must both return 200")

    discovery = evidence.get("browser_discovery")
    if not isinstance(discovery, dict) or not discovery.get("available_browsers"):
        errors.append("no supported browser was recorded")
    elif not str(discovery.get("selected_browser", "")).strip():
        errors.append("selected browser is missing")

    required_interactions = journey.get("interactions")
    interaction_evidence = evidence.get("interactions")
    if not isinstance(required_interactions, list) or not required_interactions:
        errors.append("journey has no required interactions")
        required_interactions = []
    if not isinstance(interaction_evidence, dict):
        errors.append("interaction evidence must be an object")
        interaction_evidence = {}
    for interaction in required_interactions:
        if not isinstance(interaction, dict) or not interaction.get("id"):
            errors.append("journey contains an invalid interaction")
            continue
        interaction_id = str(interaction["id"])
        observed = interaction_evidence.get(interaction_id)
        if not isinstance(observed, dict):
            errors.append(f"missing interaction evidence: {interaction_id}")
            continue
        for viewport in interaction.get("required_viewports", []):
            if observed.get(viewport) != "passed":
                errors.append(f"interaction {interaction_id} did not pass on {viewport}")

    screenshot_root_value = journey.get("screenshot_root")
    if not isinstance(screenshot_root_value, str) or not screenshot_root_value.strip():
        errors.append("journey screenshot_root is missing")
        screenshot_root = root / "__invalid_screenshot_root__"
    else:
        screenshot_root = (root / screenshot_root_value).resolve()
    viewports = journey.get("viewports") if isinstance(journey.get("viewports"), dict) else {}
    required_screenshots = (
        journey.get("required_screenshots")
        if isinstance(journey.get("required_screenshots"), dict)
        else {}
    )
    for viewport_name in ("desktop", "mobile"):
        expected_viewport = viewports.get(viewport_name)
        observed_viewport = evidence.get(viewport_name)
        if not isinstance(expected_viewport, dict):
            errors.append(f"journey viewport is missing: {viewport_name}")
            continue
        if not isinstance(observed_viewport, dict) or observed_viewport.get("status") != "passed":
            errors.append(f"{viewport_name} browser journey did not pass")
            continue
        if observed_viewport.get("viewport") != expected_viewport:
            errors.append(f"{viewport_name} viewport does not match the journey")
        screenshots = observed_viewport.get("screenshots")
        if not isinstance(screenshots, list):
            errors.append(f"{viewport_name} screenshot evidence must be a list")
            continue
        indexed = {
            str(item.get("id")): item
            for item in screenshots
            if isinstance(item, dict) and item.get("id")
        }
        for screenshot_id in required_screenshots.get(viewport_name, []):
            item = indexed.get(str(screenshot_id))
            if item is None:
                errors.append(f"missing {viewport_name} screenshot: {screenshot_id}")
                continue
            relative_path = item.get("path")
            if not isinstance(relative_path, str) or not relative_path.strip():
                errors.append(f"screenshot path is missing: {screenshot_id}")
                continue
            screenshot_path = (root / relative_path).resolve()
            if not _is_inside(screenshot_path, screenshot_root):
                errors.append(f"screenshot is outside the versioned evidence root: {screenshot_id}")
                continue
            if not screenshot_path.is_file():
                errors.append(f"screenshot file does not exist: {screenshot_id}")
                continue
            digest = hashlib.sha256(screenshot_path.read_bytes()).hexdigest()
            if item.get("sha256") != digest:
                errors.append(f"screenshot hash does not match: {screenshot_id}")
            try:
                width, height = png_dimensions(screenshot_path)
            except ValueError as error:
                errors.append(f"invalid screenshot {screenshot_id}: {error}")
                continue
            if width < int(expected_viewport["width"]) or height < int(expected_viewport["height"]):
                errors.append(f"screenshot is smaller than {viewport_name} viewport: {screenshot_id}")

    console = evidence.get("console_errors")
    if not isinstance(console, dict) or console.get("status") != "passed":
        errors.append("browser console was not observed cleanly")
    elif console.get("uncaught") != []:
        errors.append("browser console contains uncaught errors")
    overlap = evidence.get("overlap_check")
    if not isinstance(overlap, dict) or overlap.get("status") != "passed":
        errors.append("desktop/mobile overlap inspection did not pass")
    elif set(overlap.get("viewports", [])) != {"desktop", "mobile"}:
        errors.append("overlap inspection must cover desktop and mobile")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--journey", type=Path, default=DEFAULT_JOURNEY)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    args = parser.parse_args()
    errors = validate_browser_closure(
        root=args.root.resolve(),
        journey=load_json(args.journey),
        evidence=load_json(args.evidence),
    )
    print(json.dumps({"passed": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
