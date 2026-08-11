#!/usr/bin/env python3
"""Validate that hard-coded numbers in README.md match measured reality.

Pattern: standalone script, collects list[str] errors, prints them, exits 1 on
any error (same shape as validate_evolution_control.py and
validate_stage_report_template.py).

Measured values (never trust a doc for these):
  - registered block count   via agent_platform.blocks.build_block_registry()
  - collected test count     via `pytest --collect-only` (fast, ~1.5s)
  - loadable template count  via agent_platform.template_store.TemplateStore()

Known exemption: the README capability-matrix rows (49 / 35 / 18 / 100% / 91.7%)
are historical eval-script snapshots that require LLM API keys and are not
mechanically recomputable here. They should be marked as dated snapshots in the
README, not silently kept as live claims.

Usage:
  .venv/bin/python scripts/validate_doc_claims.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "platform/backend/src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT / "platform/backend"))

README_PATH = ROOT / "README.md"
TEMPLATES_DIR = ROOT / "templates"
LASTFAILED = ROOT / ".pytest_cache/v/cache/lastfailed"


def _readme() -> str:
    if not README_PATH.exists():
        raise SystemExit(f"missing {README_PATH}")
    return README_PATH.read_text(encoding="utf-8")


def collect_errors() -> list[str]:
    errors: list[str] = []

    from agent_platform.blocks import build_block_registry
    from agent_platform.template_store import TemplateStore

    measured_blocks = len(build_block_registry().list())
    measured_templates = TemplateStore().load_builtins(TEMPLATES_DIR)

    collect = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "--collect-only"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    m = re.search(r"(\d+) tests? collected", collect.stdout or "")
    measured_tests = int(m.group(1)) if m else -1
    if measured_tests < 0:
        errors.append("could not determine collected test count (pytest --collect-only failed)")

    readme_text = _readme()

    claims = [
        ("registered block count", r"(\d+)\s*个积木", measured_blocks),
        ("test passed count", r"(\d+)\s*passed\s*/\s*0\s*failed", measured_tests),
        ("behavior test count", r"(\d+)\s*项", measured_tests),
    ]
    for label, pattern, measured in claims:
        hit = re.search(pattern, readme_text)
        if not hit:
            errors.append(f"README: cannot find a claim for '{label}' (pattern: {pattern})")
            continue
        claimed = int(hit.group(1))
        if claimed != measured:
            errors.append(f"README drift [{label}]: claims {claimed}, measured {measured}")

    # Pass state without a full suite run: read the pytest lastfailed cache.
    if LASTFAILED.exists():
        try:
            failed = len(json.loads(LASTFAILED.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            failed = -1
        if failed:
            errors.append(
                f"test suite has {failed} known failing test(s) in pytest cache "
                "(fix them or xfail them before claiming a green suite)"
            )

    return errors


def main() -> int:
    errors = collect_errors()
    if not errors:
        print("OK: README hard numbers match measured reality.")
        return 0
    for err in errors:
        print(f"✗ {err}")
    print(f"\n{len(errors)} drift error(s). Reconcile README with measured reality, then re-run.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
