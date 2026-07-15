#!/usr/bin/env python3
"""Verify v0.3.53 reusable Markdown result rendering and try-run integration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".tmp" / "historical-evidence" / "v0.3.53" / "markdown_result_renderer_v0.3.53.json"


BUG_LEDGER = (
    {"id": "P1-try-run-result-small-unreadable-box", "severity": "P1", "status": "fixed", "reproduction": "Workflow try-run output was only visible through compact code/JSON boxes.", "fix": "Render run outputs through a reusable MarkdownResultCard with a full-screen readable dialog.", "verification": "workflow_run_integration_checks."},
    {"id": "P1-markdown-rendering-not-reusable", "severity": "P1", "status": "fixed", "reproduction": "Markdown rendering capability did not exist as a reusable frontend module.", "fix": "Add platform/frontend/lib/markdown.tsx with MarkdownDocument and MarkdownResultCard exports.", "verification": "markdown_module_checks."},
    {"id": "P1-user-facing-result-still-technical-first", "severity": "P1", "status": "fixed", "reproduction": "Customer result panel exposed field previews instead of a readable result document.", "fix": "Customer final result now uses the same Markdown rendering card.", "verification": "workflow_run_integration_checks."},
    {"id": "P1-markdown-renderer-xss-risk", "severity": "P1", "status": "fixed", "reproduction": "A Markdown renderer implemented with raw HTML injection would be unsafe for model outputs.", "fix": "Render React nodes without dangerouslySetInnerHTML and restrict link hrefs.", "verification": "markdown_safety_checks."},
    {"id": "P1-v0353-tests-must-enter-release-gate", "severity": "P1", "status": "fixed", "reproduction": "The Markdown output usability repair could regress if omitted from the current gate.", "fix": "Add v0.3.53 tests to docs/testing/historical/v0.3.55_regression_lanes.json.", "verification": "regression_manifest_check."},
)


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def version_at_least(version: str, floor: str) -> bool:
    def parts(value: str) -> tuple[int, int, int]:
        cleaned = value.removeprefix("v")
        major, minor, patch = cleaned.split(".")
        return int(major), int(minor), int(patch)

    try:
        return parts(version) >= parts(floor)
    except (AttributeError, ValueError):
        return False


def markdown_module_checks() -> dict[str, Any]:
    relative_path = "platform/frontend/lib/markdown.tsx"
    text = read_text(relative_path)
    required_markers = (
        "export function MarkdownDocument",
        "export function MarkdownResultCard",
        "parseMarkdownBlocks",
        "function renderInline",
        "function safeHref",
        "kind: 'table'",
        "kind: 'code'",
        "role=\"dialog\"",
        "Escape",
    )
    cases = {
        "module_exists": (ROOT / relative_path).exists(),
        "exports_reusable_document": "export function MarkdownDocument" in text,
        "exports_reusable_result_card": "export function MarkdownResultCard" in text,
        "supports_block_markdown_shapes": all(marker in text for marker in ("kind: 'heading'", "kind: 'list'", "kind: 'table'", "kind: 'code'")),
        "supports_inline_markdown_shapes": all(marker in text for marker in ("<strong", "<em", "<a ", "<code")),
        "has_click_open_dialog": all(marker in text for marker in ("setOpen(true)", "role=\"dialog\"", "markdown-dialog-backdrop")),
        "has_escape_close": "event.key === 'Escape'" in text,
        "required_markers_present": all(marker in text for marker in required_markers),
    }
    return {"id": "markdown_module_checks", "path": relative_path, "passed": all(cases.values()), "cases": cases, "required_markers": required_markers}


def markdown_safety_checks() -> dict[str, Any]:
    text = read_text("platform/frontend/lib/markdown.tsx")
    cases = {
        "does_not_use_dangerously_set_inner_html": "dangerouslySetInnerHTML" not in text,
        "links_are_sanitized": "function safeHref" in text and "mailto:" in text and "trimmed.startsWith('/')" in text,
        "raw_html_is_not_injected": "innerHTML" not in text,
        "code_fences_render_as_text_nodes": "<pre key={key}" in text and "<code>{block.code}</code>" in text,
    }
    return {"id": "markdown_safety_checks", "passed": all(cases.values()), "cases": cases}


def workflow_run_integration_checks() -> dict[str, Any]:
    relative_path = "platform/frontend/app/applications/[id]/page.tsx"
    text = read_text(relative_path)
    cases = {
        "imports_markdown_card": "import { MarkdownResultCard } from '@/lib/markdown'" in text,
        "converts_outputs_to_markdown": "function workflowRunResultMarkdown" in text and "function markdownValue" in text,
        "customer_result_uses_markdown_card": 'dataSurface="customer-run-result"' in text and "customerResultRenderedTitle" in text,
        "try_run_result_uses_markdown_card": 'data-try-result-preview="markdown-rendered-output"' in text and 'dataSurface="try-run-result"' in text,
        "raw_json_is_collapsible": "rawLabel={t.tryResultRawJsonTitle}" in text and "rawSource={tryResultRawPayload}" in text,
        "old_primary_run_output_pre_removed": "<pre>{JSON.stringify(run.outputs || run.error, null, 2)}</pre>" not in text,
        "complex_outputs_become_json_code_blocks": "markdownFence(JSON.stringify(value, null, 2), 'json')" in text,
    }
    return {"id": "workflow_run_integration_checks", "path": relative_path, "passed": all(cases.values()), "cases": cases}


def frontend_copy_checks() -> dict[str, Any]:
    text = read_text("platform/frontend/lib/i18n.ts")
    keys = (
        "customerResultRenderedTitle",
        "customerResultRenderedHelp",
        "markdownOpenRendered",
        "markdownCloseRendered",
        "tryResultRenderedTitle",
        "tryResultRenderedHelp",
        "tryResultRawJsonTitle",
        "tryResultErrorMarkdownTitle",
    )
    cases = {f"zh_en_key_{key}": text.count(key) >= 2 for key in keys}
    cases["preview_copy_mentions_rendered_first"] = "先看渲染结果" in text and "rendered result first" in text
    return {"id": "frontend_copy_checks", "path": "platform/frontend/lib/i18n.ts", "passed": all(cases.values()), "cases": cases}


def frontend_style_checks() -> dict[str, Any]:
    text = read_text("platform/frontend/app/globals.css")
    selectors = (
        ".markdown-result-card",
        ".markdown-document",
        ".markdown-dialog-backdrop",
        ".markdown-dialog",
        ".markdown-raw-details",
        ".markdown-table-wrap",
    )
    cases = {f"selector_{selector}": selector in text for selector in selectors}
    cases["dialog_is_fixed_width_independent"] = "position:fixed" in text and "width:min(900px,calc(100vw - 32px))" in text
    cases["compact_preview_has_readable_height"] = ".markdown-document.compact{max-height:230px" in text
    return {"id": "frontend_style_checks", "path": "platform/frontend/app/globals.css", "passed": all(cases.values()), "cases": cases}


def regression_manifest_check() -> dict[str, Any]:
    relative_path = "docs/testing/historical/v0.3.55_regression_lanes.json"
    manifest = json.loads(read_text(relative_path))
    current_lane = next((lane for lane in manifest.get("lanes", []) if lane.get("id") == "v0.3.x_current_release_gate"), {})
    command = current_lane.get("command", [])
    test_files = set(current_lane.get("test_files", []))
    pass_count = current_lane.get("expected", {}).get("pass_count", 0)
    cases = {
        "manifest_version_is_v0353_or_later": version_at_least(str(manifest.get("version", "")), "v0.3.53"),
        "source_stage_report_is_recorded": bool(manifest.get("source_stage_report")),
        "v0353_test_in_command": "tests/test_v03_53_markdown_result_renderer.py" in command,
        "v0353_test_in_test_files": "tests/test_v03_53_markdown_result_renderer.py" in test_files,
        "pass_count_not_less_than_v0353_floor": isinstance(pass_count, int) and pass_count >= 310,
    }
    return {"id": "regression_manifest_check", "path": relative_path, "passed": all(cases.values()), "cases": cases, "pass_count": pass_count}


def bug_ledger_evidence() -> dict[str, Any]:
    blocking = [item for item in BUG_LEDGER if item["severity"] in {"P0", "P1"} and item["status"] not in {"fixed", "verified_fixed", "deferred_with_reason"}]
    return {"id": "p0_p1_bug_ledger_markdown_result_renderer", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def build_evidence() -> dict[str, Any]:
    checks = [
        bug_ledger_evidence(),
        markdown_module_checks(),
        markdown_safety_checks(),
        workflow_run_integration_checks(),
        frontend_copy_checks(),
        frontend_style_checks(),
        regression_manifest_check(),
        {"id": "safety_no_live_side_effects_markdown_result_renderer", "passed": True, "called_endpoints": [], "model_call_used": False},
    ]
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.53",
        "stage": "markdown_result_renderer",
        "status": "passed" if not failed else "failed",
        "bug_ledger": list(BUG_LEDGER),
        "checks": checks,
        "summary": {
            "failed_check_count": len(failed),
            "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"],
            "markdown_module": markdown_module_checks(),
            "try_run_integration": workflow_run_integration_checks(),
        },
    }


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    evidence = build_evidence()
    write_evidence(args.output, evidence)
    print(json.dumps({"status": evidence["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
