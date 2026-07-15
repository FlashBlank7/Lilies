#!/usr/bin/env python3
"""Record v0.4.3 browser evidence atomically without allowing partial success."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v04_03_browser_closure_gate import (  # noqa: E402
    DEFAULT_EVIDENCE,
    DEFAULT_JOURNEY,
    load_json,
    png_dimensions,
    validate_browser_closure,
)


def save_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _blocked_copy(evidence: dict[str, Any]) -> dict[str, Any]:
    updated = deepcopy(evidence)
    updated["status"] = "blocked"
    return updated


def record_browser(evidence: dict[str, Any], browser: str) -> dict[str, Any]:
    if not browser.strip():
        raise ValueError("browser is required")
    updated = _blocked_copy(evidence)
    discovery = updated.setdefault("browser_discovery", {})
    discovery["available_browsers"] = [browser]
    discovery["selected_browser"] = browser
    discovery["get_for_url_result"] = "selected"
    return updated


def record_interaction(
    journey: dict[str, Any],
    evidence: dict[str, Any],
    *,
    interaction_id: str,
    viewport: str,
    status: str,
) -> dict[str, Any]:
    interactions = {
        str(item.get("id")): item
        for item in journey.get("interactions", [])
        if isinstance(item, dict) and item.get("id")
    }
    if interaction_id not in interactions:
        raise ValueError(f"unknown interaction: {interaction_id}")
    if viewport not in interactions[interaction_id].get("required_viewports", []):
        raise ValueError(f"interaction {interaction_id} does not require viewport {viewport}")
    if status not in {"passed", "failed"}:
        raise ValueError("interaction status must be passed or failed")
    updated = _blocked_copy(evidence)
    updated.setdefault("interactions", {}).setdefault(interaction_id, {})[viewport] = status
    return updated


def record_screenshot(
    root: Path,
    journey: dict[str, Any],
    evidence: dict[str, Any],
    *,
    screenshot_id: str,
    viewport: str,
    path: Path,
) -> dict[str, Any]:
    required = journey.get("required_screenshots", {}).get(viewport, [])
    if screenshot_id not in required:
        raise ValueError(f"unknown {viewport} screenshot: {screenshot_id}")
    absolute = path.resolve()
    screenshot_root = (root / str(journey["screenshot_root"])).resolve()
    if screenshot_root != absolute and screenshot_root not in absolute.parents:
        raise ValueError("screenshot must be inside the versioned screenshot root")
    width, height = png_dimensions(absolute)
    expected = journey["viewports"][viewport]
    if width < int(expected["width"]) or height < int(expected["height"]):
        raise ValueError(f"screenshot is smaller than the {viewport} viewport")
    item = {
        "id": screenshot_id,
        "path": absolute.relative_to(root.resolve()).as_posix(),
        "sha256": hashlib.sha256(absolute.read_bytes()).hexdigest(),
    }
    updated = _blocked_copy(evidence)
    surface = updated.setdefault(viewport, {})
    surface["viewport"] = expected
    screenshots = surface.setdefault("screenshots", [])
    surface["screenshots"] = [entry for entry in screenshots if entry.get("id") != screenshot_id]
    surface["screenshots"].append(item)
    return updated


def record_console(evidence: dict[str, Any], uncaught: list[str]) -> dict[str, Any]:
    updated = _blocked_copy(evidence)
    updated["console_errors"] = {
        "status": "failed" if uncaught else "passed",
        "uncaught": uncaught,
    }
    return updated


def record_overlap(evidence: dict[str, Any], *, status: str) -> dict[str, Any]:
    if status not in {"passed", "failed"}:
        raise ValueError("overlap status must be passed or failed")
    updated = _blocked_copy(evidence)
    updated["overlap_check"] = {
        "status": status,
        "viewports": ["desktop", "mobile"],
    }
    return updated


def finalize(
    *,
    root: Path,
    journey: dict[str, Any],
    evidence: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    candidate = deepcopy(evidence)
    candidate["status"] = "passed"
    for viewport in ("desktop", "mobile"):
        surface = candidate.get(viewport)
        if isinstance(surface, dict) and surface.get("screenshots"):
            surface["status"] = "passed"
    errors = validate_browser_closure(root=root, journey=journey, evidence=candidate)
    return (candidate if not errors else _blocked_copy(evidence), errors)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--journey", type=Path, default=DEFAULT_JOURNEY)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    commands = parser.add_subparsers(dest="command", required=True)
    browser_command = commands.add_parser("browser")
    browser_command.add_argument("--name", required=True)
    interaction_command = commands.add_parser("interaction")
    interaction_command.add_argument("--id", required=True)
    interaction_command.add_argument("--viewport", choices=("desktop", "mobile"), required=True)
    interaction_command.add_argument("--status", choices=("passed", "failed"), required=True)
    screenshot_command = commands.add_parser("screenshot")
    screenshot_command.add_argument("--id", required=True)
    screenshot_command.add_argument("--viewport", choices=("desktop", "mobile"), required=True)
    screenshot_command.add_argument("--path", type=Path, required=True)
    console_command = commands.add_parser("console")
    console_command.add_argument("--uncaught", action="append", default=[])
    overlap_command = commands.add_parser("overlap")
    overlap_command.add_argument("--status", choices=("passed", "failed"), required=True)
    commands.add_parser("finalize")
    args = parser.parse_args()

    root = args.root.resolve()
    journey = load_json(args.journey)
    evidence = load_json(args.evidence)
    if args.command == "browser":
        updated = record_browser(evidence, args.name)
    elif args.command == "interaction":
        updated = record_interaction(
            journey,
            evidence,
            interaction_id=args.id,
            viewport=args.viewport,
            status=args.status,
        )
    elif args.command == "screenshot":
        updated = record_screenshot(
            root,
            journey,
            evidence,
            screenshot_id=args.id,
            viewport=args.viewport,
            path=args.path,
        )
    elif args.command == "console":
        updated = record_console(evidence, args.uncaught)
    elif args.command == "overlap":
        updated = record_overlap(evidence, status=args.status)
    else:
        updated, errors = finalize(root=root, journey=journey, evidence=evidence)
        if errors:
            print(json.dumps({"passed": False, "errors": errors}, ensure_ascii=False, indent=2))
            return 1
    save_json_atomic(args.evidence, updated)
    print(json.dumps({"status": updated["status"], "evidence": str(args.evidence)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
