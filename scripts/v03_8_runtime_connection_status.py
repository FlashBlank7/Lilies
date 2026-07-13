#!/usr/bin/env python3
"""Verify v0.3.8 truthful runtime connection status surfaces."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "workingon" / "runtime_connection_status_v0.3.8.json"
SMOKE_MARKER = "v0.3.8-smoke"
EXPECTED_RUNTIME_VERSION = "v0.3.6"


BUG_LEDGER = (
    {
        "id": "P1-static-connected-status",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "The home topbar said the environment was connected without reading runtime health.",
        "fix": "Add a health/auth-derived runtime status badge with data-runtime-status.",
        "verification": "home_runtime_status_markers and rendered_home_runtime_status.",
    },
    {
        "id": "P1-detail-runtime-status-missing",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "The detail page did not show whether the studio was connected to current backend code.",
        "fix": "Add a compact runtime chip to the detail header.",
        "verification": "detail_runtime_status_markers and rendered_detail_runtime_status.",
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
    for path in env_files or (
        ROOT / ".env",
        ROOT / "platform" / "backend" / ".env",
        ROOT / "platform" / "frontend" / ".env.local",
    ):
        values = parse_env_file(path)
        for key in ("AGENT_PLATFORM_API_TOKEN", "AGENT_PLATFORM_TOKEN", "API_TOKEN"):
            if values.get(key):
                return values[key], f"file:{display_path(path)}:{key}"
    return "", "missing"


def request_json(method: str, url: str, *, token: str = "", payload: dict[str, object] | None = None) -> dict[str, object]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json", "User-Agent": "Lilies-v0.3.8-runtime-status"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8", errors="replace")
        return {"status_code": response.getcode(), "json": json.loads(body), "error": ""}


def fetch_text(url: str) -> dict[str, object]:
    request = urllib.request.Request(url, headers={"User-Agent": "Lilies-v0.3.8-runtime-status"})
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8", errors="replace")
        return {"status_code": response.getcode(), "body": body[:60000], "error": ""}


def failed_check(check_id: str, url: str, error: Exception) -> dict[str, object]:
    status_code = error.code if isinstance(error, urllib.error.HTTPError) else 0
    return {"id": check_id, "url": url, "passed": False, "status_code": status_code, "error": str(error)}


def source_marker_checks() -> list[dict[str, object]]:
    checks = [
        (
            "runtime_status_classifier_markers",
            "platform/frontend/lib/runtime-status.ts",
            ("classifyRuntimeStatus", "auth_required", "stale", "unavailable", "expectedRuntimeVersion"),
        ),
        (
            "home_runtime_status_markers",
            "platform/frontend/app/page.tsx",
            ("refreshRuntimeStatus", "data-runtime-status", "runtimeStatusText", "runtimeStatusDetail"),
        ),
        (
            "detail_runtime_status_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            ("refreshRuntimeStatus", "data-runtime-status", "runtime-chip", "runtimeStatusText"),
        ),
        (
            "runtime_status_copy_markers",
            "platform/frontend/lib/i18n.ts",
            ("runtimeStatusConnected", "runtimeStatusAuthRequired", "runtimeStatusStale", "runtimeStatusUnavailable"),
        ),
        (
            "runtime_status_style_markers",
            "platform/frontend/app/globals.css",
            (".runtime-status", ".runtime-chip", ".runtime-chip.unavailable"),
        ),
    ]
    evidence: list[dict[str, object]] = []
    for check_id, relative_path, markers in checks:
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        missing = [marker for marker in markers if marker not in text]
        evidence.append({"id": check_id, "path": relative_path, "required_markers": list(markers), "missing_markers": missing, "passed": not missing})
    return evidence


def bug_ledger_evidence() -> dict[str, object]:
    blocking = [
        item
        for item in BUG_LEDGER
        if item["severity"] in {"P0", "P1"} and item["status"] not in {"fixed", "verified_fixed", "deferred_with_reason"}
    ]
    return {"id": "p0_p1_bug_ledger_runtime_connection_status", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def create_payload(now: int | None = None) -> dict[str, str]:
    suffix = now if now is not None else int(time.time())
    requirement = f"[{SMOKE_MARKER}] Runtime connection status smoke app. Create and clean without starting a build."
    return {
        "name": f"{SMOKE_MARKER} runtime status {suffix}",
        "description": requirement,
        "requirement": requirement,
        "mode": "workflow",
    }


def runtime_health_check(api_url: str) -> dict[str, object]:
    url = api_url.rstrip("/") + "/health"
    try:
        result = request_json("GET", url)
        runtime = result["json"].get("runtime", {}) if isinstance(result["json"], dict) else {}
        return {
            "id": "runtime_health_current_code",
            "url": url,
            "passed": result["status_code"] == 200 and runtime.get("version") == EXPECTED_RUNTIME_VERSION and runtime.get("current_code_ready") is True,
            "status_code": result["status_code"],
            "runtime": runtime,
        }
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        return failed_check("runtime_health_current_code", url, error)


def live_checks(api_url: str, frontend_url: str, token: str) -> tuple[list[dict[str, object]], dict[str, object]]:
    api_base = api_url.rstrip("/")
    frontend_base = frontend_url.rstrip("/")
    checks: list[dict[str, object]] = [runtime_health_check(api_base)]
    safety: dict[str, object] = {
        "build_endpoint_called": False,
        "called_endpoints": [],
        "smoke_marker": SMOKE_MARKER,
        "cleanup_attempted": False,
    }
    try:
        home = fetch_text(frontend_base + "/")
        home_body = str(home["body"])
        checks.append({
            "id": "rendered_home_runtime_status",
            "url": frontend_base + "/",
            "passed": home["status_code"] == 200 and "data-runtime-status" in home_body and "runtime-status" in home_body,
            "status_code": home["status_code"],
        })
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        checks.append(failed_check("rendered_home_runtime_status", frontend_base + "/", error))

    application_id = ""
    try:
        created = request_json("POST", api_base + "/api/v1/applications", token=token, payload=create_payload())
        app = created["json"]
        application_id = str(app.get("id", ""))
        safety["called_endpoints"].append("POST /api/v1/applications")
        checks.append({"id": "created_runtime_status_smoke_app", "passed": created["status_code"] == 201 and bool(application_id), "status_code": created["status_code"], "application_id": application_id})
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        checks.append(failed_check("created_runtime_status_smoke_app", api_base + "/api/v1/applications", error))

    if application_id:
        detail_url = frontend_base + f"/applications/{application_id}?safeDraft=1"
        try:
            detail = fetch_text(detail_url)
            detail_body = str(detail["body"])
            checks.append({
                "id": "rendered_detail_runtime_status",
                "url": detail_url,
                "passed": detail["status_code"] == 200 and "data-runtime-status" in detail_body and "runtime-chip" in detail_body,
                "status_code": detail["status_code"],
            })
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            checks.append(failed_check("rendered_detail_runtime_status", detail_url, error))

        cleanup_url = api_base + f"/api/v1/applications/{application_id}/smoke-cleanup"
        try:
            cleanup = request_json("POST", cleanup_url, token=token, payload={"smoke_marker": SMOKE_MARKER, "dry_run": False})
            safety["cleanup_attempted"] = True
            safety["called_endpoints"].append("POST /api/v1/applications/{id}/smoke-cleanup")
            checks.append({"id": "cleanup_smoke_app", "passed": cleanup["status_code"] == 200 and cleanup["json"].get("deleted") is True, "status_code": cleanup["status_code"], "related_counts": cleanup["json"].get("related_counts")})
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            checks.append(failed_check("cleanup_smoke_app", cleanup_url, error))

        try:
            request_json("GET", api_base + f"/api/v1/applications/{application_id}", token=token)
            checks.append({"id": "verified_smoke_deleted", "passed": False, "error": "smoke app still exists"})
        except urllib.error.HTTPError as error:
            checks.append({"id": "verified_smoke_deleted", "passed": error.code == 404, "status_code": error.code})
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            checks.append(failed_check("verified_smoke_deleted", api_base + f"/api/v1/applications/{application_id}", error))

    checks.append({"id": "safety_no_build_call", "passed": safety["build_endpoint_called"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint": "POST /api/v1/applications/{id}/builds"})
    return checks, safety


def build_evidence(
    *,
    live: bool = False,
    api_url: str = "http://127.0.0.1:8001",
    frontend_url: str = "http://127.0.0.1:3000",
    token: str = "",
) -> dict[str, object]:
    checks: list[dict[str, object]] = [bug_ledger_evidence(), *source_marker_checks()]
    loaded_token, token_source = load_token(token)
    safety: dict[str, object] = {"build_endpoint_called": False, "called_endpoints": [], "smoke_marker": SMOKE_MARKER}
    if live:
        if not loaded_token:
            checks.append({"id": "token_available", "passed": False, "token_source": token_source})
        else:
            checks.append({"id": "token_available", "passed": True, "token_source": token_source})
            live_result, safety = live_checks(api_url, frontend_url, loaded_token)
            checks.extend(live_result)
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.8",
        "stage": "runtime_connection_status_surface",
        "status": "passed" if not failed else "failed",
        "live_checks_enabled": live,
        "token_source": token_source if live else "not_used",
        "smoke_marker": SMOKE_MARKER,
        "bug_ledger": list(BUG_LEDGER),
        "safety": safety,
        "checks": checks,
        "summary": {
            "failed_check_count": len(failed),
            "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"],
            "build_endpoint_called": safety["build_endpoint_called"],
        },
    }


def write_evidence(path: Path, evidence: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.8 runtime connection status evidence.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--api-url", default="http://127.0.0.1:8001")
    parser.add_argument("--frontend-url", default="http://127.0.0.1:3000")
    parser.add_argument("--token", default="")
    args = parser.parse_args()

    evidence = build_evidence(live=args.live, api_url=args.api_url, frontend_url=args.frontend_url, token=args.token)
    write_evidence(args.output, evidence)
    print(json.dumps({"status": evidence["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
