#!/usr/bin/env python3
"""Record v0.3.4 rendered fallback evidence and smoke app retention index."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".tmp" / "historical-evidence" / "v0.3.4" / "browser_unavailable_smoke_retention_v0.3.4.json"
SMOKE_RE = re.compile(r"v0\.3\.\d+-smoke")


BUG_LEDGER = (
    {
        "id": "P1-browser-runtime-unavailable",
        "severity": "P1",
        "status": "deferred_with_reason",
        "reproduction": "Browser skill runtime initialized but returned no available browsers.",
        "fix": "Record unavailable status and use rendered-route fallback evidence for this stage.",
        "verification": "browser_status and rendered_home/detail checks in v0.3.4 evidence.",
    },
    {
        "id": "P1-smoke-apps-not-indexed",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Automatic evolution leaves local smoke apps that are not easy to inventory.",
        "fix": "List applications and group smoke evidence apps by marker.",
        "verification": "smoke_retention_index check records marker counts and latest ids.",
    },
    {
        "id": "P1-smoke-app-delete-absent",
        "severity": "P1",
        "status": "deferred_with_reason",
        "reproduction": "There is no application delete/archive API for smoke cleanup.",
        "fix": "Keep retention index now; defer cleanup/archive implementation to a dedicated stage.",
        "verification": "retention_policy records no deletion attempted.",
    },
)


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def load_token(explicit: str = "", env_files: Iterable[Path] | None = None) -> tuple[str, str]:
    if explicit.strip():
        return explicit.strip(), "argument"
    for key in ("AGENT_PLATFORM_API_TOKEN", "AGENT_PLATFORM_TOKEN", "API_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value:
            return value, f"env:{key}"
    for path in env_files or (ROOT / ".env", ROOT / "platform" / "backend" / ".env", ROOT / "platform" / "frontend" / ".env.local"):
        values = parse_env_file(path)
        for key in ("AGENT_PLATFORM_API_TOKEN", "AGENT_PLATFORM_TOKEN", "API_TOKEN"):
            if values.get(key):
                return values[key], f"file:{display_path(path)}:{key}"
    return "", "missing"


def request_json(url: str, *, token: str) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "User-Agent": "Lilies-v0.3.4-retention"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8", errors="replace")
        return {"status_code": response.getcode(), "json": json.loads(body), "error": ""}


def fetch_html(url: str) -> dict[str, object]:
    request = urllib.request.Request(url, headers={"User-Agent": "Lilies-v0.3.4-retention"})
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8", errors="replace")
        return {"status_code": response.getcode(), "body": body[:50000], "error": ""}


def source_marker_checks() -> list[dict[str, object]]:
    checks = [
        ("safe_draft_home_source", "platform/frontend/app/page.tsx", ("saveDraftOnly", "seedSafeDraftSkeleton", "customer-intake-panel")),
        ("safe_draft_copy_source", "platform/frontend/lib/i18n.ts", ("customerExamples", "saveDraftOnlyButton", "safeDraftHint")),
    ]
    evidence: list[dict[str, object]] = []
    for check_id, relative_path, markers in checks:
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        missing = [marker for marker in markers if marker not in text]
        evidence.append({"id": check_id, "path": relative_path, "required_markers": list(markers), "missing_markers": missing, "passed": not missing})
    return evidence


def smoke_marker(app: dict[str, object]) -> str | None:
    text = f"{app.get('name', '')}\n{app.get('description', '')}\n{app.get('requirement', '')}"
    match = SMOKE_RE.search(text)
    return match.group(0) if match else None


def smoke_retention_index(applications: list[dict[str, object]]) -> dict[str, object]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for app in applications:
        marker = smoke_marker(app)
        if not marker:
            continue
        grouped.setdefault(marker, []).append(app)
    markers = {
        marker: {
            "count": len(items),
            "latest_id": str(items[0].get("id", "")) if items else "",
            "latest_name": str(items[0].get("name", "")) if items else "",
        }
        for marker, items in sorted(grouped.items())
    }
    return {
        "id": "smoke_retention_index",
        "passed": bool(markers),
        "marker_count": len(markers),
        "total_smoke_app_count": sum(item["count"] for item in markers.values()),
        "markers": markers,
    }


def bug_ledger_evidence() -> dict[str, object]:
    blocking = [
        item for item in BUG_LEDGER
        if item["severity"] in {"P0", "P1"} and item["status"] not in {"fixed", "verified_fixed", "deferred_with_reason"}
    ]
    return {"id": "p0_p1_bug_ledger_browser_retention", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def rendered_checks(frontend_url: str, latest_smoke_id: str) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []
    home_url = frontend_url.rstrip("/") + "/"
    try:
        home = fetch_html(home_url)
        body = str(home["body"])
        required = ["customer-intake-panel", "secondary-action", "仅保存草稿"]
        checks.append({
            "id": "rendered_home_safe_draft",
            "url": home_url,
            "passed": home["status_code"] == 200 and all(marker in body for marker in required),
            "status_code": home["status_code"],
            "required_markers": required,
            "missing_markers": [marker for marker in required if marker not in body],
            "error": "",
        })
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        checks.append({"id": "rendered_home_safe_draft", "url": home_url, "passed": False, "status_code": 0, "error": str(error)})
    if latest_smoke_id:
        detail_url = frontend_url.rstrip("/") + f"/applications/{latest_smoke_id}?safeDraft=1"
        try:
            detail = fetch_html(detail_url)
            body = str(detail["body"])
            checks.append({
                "id": "rendered_detail_route",
                "url": detail_url,
                "passed": detail["status_code"] == 200 and "Foundry" in body,
                "status_code": detail["status_code"],
                "required_markers": ["Foundry"],
                "missing_markers": [] if "Foundry" in body else ["Foundry"],
                "error": "",
            })
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            checks.append({"id": "rendered_detail_route", "url": detail_url, "passed": False, "status_code": 0, "error": str(error)})
    return checks


def build_evidence(
    *,
    live: bool = False,
    api_url: str = "http://127.0.0.1:8001",
    frontend_url: str = "http://127.0.0.1:3000",
    token: str = "",
    applications: list[dict[str, object]] | None = None,
    browser_status: str = "unavailable",
    browser_note: str = "Browser runtime returned no available browsers.",
) -> dict[str, object]:
    checks: list[dict[str, object]] = [
        bug_ledger_evidence(),
        {"id": "browser_status", "passed": browser_status in {"available", "unavailable"}, "status": browser_status, "note": browser_note},
        *source_marker_checks(),
    ]
    loaded_token, token_source = load_token(token)
    apps = applications or []
    if live:
        if not loaded_token:
            checks.append({"id": "token_available", "passed": False, "token_source": token_source})
        else:
            checks.append({"id": "token_available", "passed": True, "token_source": token_source})
            try:
                result = request_json(api_url.rstrip("/") + "/api/v1/applications", token=loaded_token)
                apps = result["json"] if isinstance(result["json"], list) else []  # type: ignore[assignment]
                checks.append({"id": "listed_applications", "passed": result["status_code"] == 200, "status_code": result["status_code"], "application_count": len(apps)})
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
                checks.append({"id": "listed_applications", "passed": False, "status_code": 0, "error": str(error)})
    retention = smoke_retention_index(apps)
    checks.append(retention)
    latest_ids = [
        data["latest_id"]
        for data in retention.get("markers", {}).values()  # type: ignore[union-attr]
        if isinstance(data, dict) and data.get("latest_id")
    ]
    latest_smoke_id = latest_ids[-1] if latest_ids else ""
    if live:
        checks.extend(rendered_checks(frontend_url, latest_smoke_id))
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.4",
        "stage": "browser_flow_and_smoke_retention",
        "status": "passed" if not failed else "failed",
        "browser_evidence": {
            "status": browser_status,
            "note": browser_note,
            "claim": "fallback_rendered_route_evidence_only" if browser_status == "unavailable" else "browser_available",
        },
        "token_source": token_source if live else "not_used",
        "retention_policy": {
            "status": "indexed_retention_no_delete_attempted",
            "reason": "Application delete/archive API is absent; smoke apps are indexed by marker for traceability.",
        },
        "checks": checks,
        "summary": {
            "failed_check_count": len(failed),
            "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"],
            "smoke_app_count": retention["total_smoke_app_count"],
            "smoke_marker_count": retention["marker_count"],
        },
    }


def write_evidence(path: Path, evidence: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Record browser-unavailable fallback and smoke retention evidence.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--api-url", default="http://127.0.0.1:8001")
    parser.add_argument("--frontend-url", default="http://127.0.0.1:3000")
    parser.add_argument("--token", default="")
    parser.add_argument("--browser-status", default="unavailable", choices=["available", "unavailable"])
    parser.add_argument("--browser-note", default="Browser runtime returned no available browsers.")
    args = parser.parse_args()
    evidence = build_evidence(
        live=args.live,
        api_url=args.api_url,
        frontend_url=args.frontend_url,
        token=args.token,
        browser_status=args.browser_status,
        browser_note=args.browser_note,
    )
    write_evidence(args.output, evidence)
    print(json.dumps({"status": evidence["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
