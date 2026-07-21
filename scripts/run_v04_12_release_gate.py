#!/usr/bin/env python3
"""Run and persist the reproducible v0.4.12 release gate."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "evidence" / "v0.4.12" / "release-gate.json"
NODE_BIN = Path.home() / ".nvm" / "versions" / "node" / "v24.15.0" / "bin"


COMMANDS = [
    (
        "focused_backend",
        ".venv/bin/python -m pytest -q tests/test_v04_12_openapi_connector_generation.py tests/test_v04_10_connector_embedding.py",
        ROOT,
    ),
    ("full_backend", ".venv/bin/python -m pytest -q", ROOT),
    (
        "ruff_changed_files",
        ".venv/bin/python -m ruff check platform/backend/src/agent_platform/openapi_connector.py platform/backend/src/agent_platform/connector_sdk.py platform/backend/src/agent_platform/api.py scripts/run_v04_12_openapi_generalization.py scripts/run_v04_12_openapi_live_contract.py scripts/validate_v04_12_openapi_evidence.py scripts/run_v04_12_release_gate.py tests/test_v04_12_openapi_connector_generation.py",
        ROOT,
    ),
    ("frontend_typecheck", "npm run lint", ROOT / "platform" / "frontend"),
    ("frontend_production_build", "npm run build", ROOT / "platform" / "frontend"),
    (
        "evidence_denominator",
        ".venv/bin/python scripts/validate_v04_12_openapi_evidence.py",
        ROOT,
    ),
    (
        "stage_report_template",
        ".venv/bin/python scripts/validate_stage_report_template.py docs/stage-reports/v0.4.12_openapi_connector_generation_loop.md",
        ROOT,
    ),
    (
        "evolution_control",
        ".venv/bin/python scripts/validate_evolution_control.py",
        ROOT,
    ),
]


def run_command(name: str, command: str, cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        shell=True,
        text=True,
        capture_output=True,
        check=False,
    )
    output = completed.stdout + completed.stderr
    return {
        "name": name,
        "command": command,
        "cwd": str(cwd.relative_to(ROOT) or "."),
        "status": "passed" if completed.returncode == 0 else "failed",
        "exit_code": completed.returncode,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
        "output_tail": output[-4000:],
    }


def main() -> int:
    env = os.environ.copy()
    env["PATH"] = f"{NODE_BIN}:{env.get('PATH', '')}"
    results = [run_command(name, command, cwd, env) for name, command, cwd in COMMANDS]
    payload = {
        "schema_version": "v0.4.12-release-gate-1",
        "status": "passed" if all(item["status"] == "passed" for item in results) else "failed",
        "commands": results,
        "browser_evidence": "browser/browser-evidence.json",
        "browser_execution_command": "node scripts/v04_12_openapi_connector_browser.mjs --api http://127.0.0.1:18120 --web http://127.0.0.1:13120 --token v0412-browser --output docs/evidence/v0.4.12/browser",
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "output": str(OUTPUT)}, ensure_ascii=False))
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
