from __future__ import annotations

import hashlib
import json
import struct
import zlib
from pathlib import Path

from scripts.v04_03_browser_closure_gate import load_json, validate_browser_closure


ROOT = Path(__file__).resolve().parents[1]
JOURNEY = load_json(ROOT / "docs/workingon/v0.4.3_browser_journey.json")


def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + (b"\xff\xff\xff" * width) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _passing_evidence(root: Path) -> dict:
    evidence = {
        "schema_version": "1.0",
        "stage": "v0.4.3",
        "task_id": "V04-03-T01F",
        "status": "passed",
        "runtime_health": {"status": "ok", "current_code_ready": True},
        "frontend_runtime": {
            "status": "ready",
            "http_status": 200,
            "proxy_fixture_status": 200,
        },
        "browser_discovery": {
            "available_browsers": ["iab"],
            "selected_browser": "iab",
        },
        "interactions": {},
        "console_errors": {"status": "passed", "uncaught": []},
        "overlap_check": {"status": "passed", "viewports": ["desktop", "mobile"]},
    }
    for interaction in JOURNEY["interactions"]:
        evidence["interactions"][interaction["id"]] = {
            viewport: "passed" for viewport in interaction["required_viewports"]
        }
    for viewport_name, viewport in JOURNEY["viewports"].items():
        screenshots = []
        for screenshot_id in JOURNEY["required_screenshots"][viewport_name]:
            relative = Path(JOURNEY["screenshot_root"]) / viewport_name / f"{screenshot_id}.png"
            absolute = root / relative
            absolute.parent.mkdir(parents=True, exist_ok=True)
            absolute.write_bytes(_png(viewport["width"], viewport["height"]))
            screenshots.append(
                {
                    "id": screenshot_id,
                    "path": relative.as_posix(),
                    "sha256": hashlib.sha256(absolute.read_bytes()).hexdigest(),
                }
            )
        evidence[viewport_name] = {
            "status": "passed",
            "viewport": viewport,
            "screenshots": screenshots,
        }
    return evidence


def test_current_browser_evidence_is_rejected_until_real_browser_work_finishes() -> None:
    evidence = load_json(ROOT / "docs/workingon/v0.4.3_browser_verification.json")

    errors = validate_browser_closure(root=ROOT, journey=JOURNEY, evidence=evidence)

    assert "browser evidence status must be passed" in errors
    assert "no supported browser was recorded" in errors
    assert "desktop browser journey did not pass" in errors
    assert "mobile browser journey did not pass" in errors
    assert "browser console was not observed cleanly" in errors


def test_complete_browser_evidence_requires_real_hashed_pngs(tmp_path: Path) -> None:
    evidence = _passing_evidence(tmp_path)

    assert validate_browser_closure(root=tmp_path, journey=JOURNEY, evidence=evidence) == []

    evidence["desktop"]["screenshots"][0]["sha256"] = "tampered"
    errors = validate_browser_closure(root=tmp_path, journey=JOURNEY, evidence=evidence)
    assert "screenshot hash does not match: mode-and-stale-evidence" in errors


def test_browser_gate_cli_defaults_point_to_active_evidence() -> None:
    source = (ROOT / "scripts/v04_03_browser_closure_gate.py").read_text(encoding="utf-8")
    studio = (ROOT / "platform/frontend/app/applications/[id]/page.tsx").read_text(
        encoding="utf-8"
    )
    journey = json.loads(
        (ROOT / "docs/workingon/v0.4.3_browser_journey.json").read_text(encoding="utf-8")
    )

    assert "docs/workingon/v0.4.3_browser_journey.json" in source
    assert "docs/workingon/v0.4.3_browser_verification.json" in source
    assert {item["id"] for item in journey["interactions"]} == {
        "mode_policy",
        "stale_evidence_publish",
        "acceptance_progress_repair",
        "schema_block_forms",
        "governed_publication_block",
        "canvas_and_console_stability",
    }
    for selector in (
        'data-studio-tab={item}',
        'data-delivery-mode-option={option.id}',
        'data-publication-action="open"',
        'data-acceptance-action="run-all"',
        'data-acceptance-repair-action="preview"',
        'data-acceptance-repair-action="apply"',
        'data-config-editor-action="save"',
    ):
        assert selector in studio
