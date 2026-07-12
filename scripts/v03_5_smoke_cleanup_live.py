#!/usr/bin/env python3
"""Exercise v0.3.5 smoke cleanup boundary against local services."""

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
DEFAULT_OUTPUT = ROOT / "docs" / "workingon" / "smoke_cleanup_boundary_v0.3.5.json"
SMOKE_MARKER = "v0.3.5-smoke"


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


def request_json(method: str, url: str, *, token: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Lilies-v0.3.5-smoke-cleanup",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8", errors="replace")
        return {"status_code": response.getcode(), "json": json.loads(body), "error": ""}


def create_payload() -> dict[str, str]:
    requirement = f"[{SMOKE_MARKER}] temporary cleanup boundary evidence app"
    return {
        "name": f"{SMOKE_MARKER} cleanup boundary {int(time.time())}",
        "description": requirement,
        "requirement": requirement,
        "mode": "workflow",
    }


def failed_check(check_id: str, error: Exception) -> dict[str, object]:
    return {"id": check_id, "passed": False, "error": str(error), "status_code": 0}


def build_evidence(*, api_url: str = "http://127.0.0.1:8001", token: str = "") -> dict[str, object]:
    checks: list[dict[str, object]] = []
    loaded_token, token_source = load_token(token)
    safety = {"build_endpoint_called": False, "called_endpoints": []}
    if not loaded_token:
        checks.append({"id": "token_available", "passed": False, "token_source": token_source})
        failed = [check for check in checks if not check["passed"]]
        return {
            "version": "v0.3.5",
            "stage": "smoke_archive_cleanup_boundary",
            "status": "failed",
            "token_source": token_source,
            "safety": safety,
            "checks": checks,
            "summary": {"failed_check_count": len(failed), "build_endpoint_called": False},
        }
    checks.append({"id": "token_available", "passed": True, "token_source": token_source})
    base = api_url.rstrip()
    app_id = ""
    try:
        created = request_json("POST", base + "/api/v1/applications", token=loaded_token, payload=create_payload())
        app = created["json"]
        app_id = str(app.get("id", ""))
        checks.append({"id": "created_smoke_app", "passed": created["status_code"] == 201 and bool(app_id), "status_code": created["status_code"], "application_id": app_id})
        safety["called_endpoints"].append("POST /api/v1/applications")
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        checks.append(failed_check("created_smoke_app", error))

    if app_id:
        cleanup_url = base + f"/api/v1/applications/{app_id}/smoke-cleanup"
        try:
            dry_run = request_json("POST", cleanup_url, token=loaded_token, payload={"smoke_marker": SMOKE_MARKER, "dry_run": True})
            checks.append({
                "id": "dry_run_cleanup",
                "passed": dry_run["status_code"] == 200 and dry_run["json"].get("deleted") is False,
                "status_code": dry_run["status_code"],
                "related_counts": dry_run["json"].get("related_counts"),
            })
            safety["called_endpoints"].append("POST /api/v1/applications/{id}/smoke-cleanup:dry_run")
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            checks.append(failed_check("dry_run_cleanup", error))
        try:
            deleted = request_json("POST", cleanup_url, token=loaded_token, payload={"smoke_marker": SMOKE_MARKER, "dry_run": False})
            checks.append({
                "id": "delete_cleanup",
                "passed": deleted["status_code"] == 200 and deleted["json"].get("deleted") is True,
                "status_code": deleted["status_code"],
            })
            safety["called_endpoints"].append("POST /api/v1/applications/{id}/smoke-cleanup:delete")
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            checks.append(failed_check("delete_cleanup", error))
        try:
            request_json("GET", base + f"/api/v1/applications/{app_id}", token=loaded_token)
            checks.append({"id": "verified_deleted", "passed": False, "error": "application still exists"})
        except urllib.error.HTTPError as error:
            checks.append({"id": "verified_deleted", "passed": error.code == 404, "status_code": error.code})
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            checks.append(failed_check("verified_deleted", error))

    checks.append({"id": "safety_no_build_call", "passed": safety["build_endpoint_called"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint": "POST /api/v1/applications/{id}/builds"})
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.5",
        "stage": "smoke_archive_cleanup_boundary",
        "status": "passed" if not failed else "failed",
        "token_source": token_source,
        "smoke_marker": SMOKE_MARKER,
        "safety": safety,
        "checks": checks,
        "summary": {"failed_check_count": len(failed), "build_endpoint_called": safety["build_endpoint_called"]},
    }


def write_evidence(path: Path, evidence: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.5 smoke cleanup live evidence.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--api-url", default="http://127.0.0.1:8001")
    parser.add_argument("--token", default="")
    args = parser.parse_args()
    evidence = build_evidence(api_url=args.api_url, token=args.token)
    write_evidence(args.output, evidence)
    print(json.dumps({"status": evidence["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
