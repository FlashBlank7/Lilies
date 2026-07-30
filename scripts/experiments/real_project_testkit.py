#!/usr/bin/env python3
"""Small public-API test kit for repeatable real-project workflow acceptance."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any | None = None,
    timeout: float = 60,
) -> Any:
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"{method} {url} returned HTTP {error.code}: {detail}"
        ) from error
    return json.loads(payload) if payload else None


def platform_json(
    method: str,
    base_url: str,
    token: str,
    path: str,
    body: Any | None = None,
) -> Any:
    return http_json(
        method,
        f"{base_url.rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {token}"},
        body=body,
    )


def run_workflow(
    *,
    base_url: str,
    token: str,
    application_id: str,
    version: int,
    inputs: dict[str, Any],
    workspace_path: str = ".",
    resume_values: dict[str, Any] | None = None,
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    created = platform_json(
        "POST",
        base_url,
        token,
        f"/api/v1/applications/{application_id}/runs",
        {
            "inputs": inputs,
            "version": version,
            "workspace_path": workspace_path,
        },
    )
    run_id = str(created["run_id"])
    resume_count = 0
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        run = platform_json("GET", base_url, token, f"/api/v1/runs/{run_id}")
        if run["status"] == "paused":
            if resume_values is None:
                raise RuntimeError(
                    f"run {run_id} paused without an acceptance response"
                )
            platform_json(
                "POST",
                base_url,
                token,
                f"/api/v1/runs/{run_id}/resume",
                {"values": resume_values},
            )
            resume_count += 1
            resume_values = None
        elif run["status"] not in {"queued", "running"}:
            run["resume_count"] = resume_count
            return run
        time.sleep(0.05)
    raise TimeoutError(f"run {run_id} did not finish within {timeout_seconds}s")


def wait_run(
    *,
    base_url: str,
    token: str,
    run_id: str,
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        run = platform_json("GET", base_url, token, f"/api/v1/runs/{run_id}")
        if run["status"] not in {"queued", "running", "paused"}:
            return run
        time.sleep(0.05)
    raise TimeoutError(f"run {run_id} did not finish within {timeout_seconds}s")


def wait_timer(
    *,
    base_url: str,
    token: str,
    timer_key: str,
    statuses: set[str],
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    encoded_key = urllib.parse.quote(timer_key, safe="")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        timer = platform_json(
            "GET",
            base_url,
            token,
            f"/api/v1/event-timers/{encoded_key}",
        )
        if timer.get("status") in statuses:
            return timer
        time.sleep(0.05)
    raise TimeoutError(
        f"timer {timer_key} did not reach {sorted(statuses)} "
        f"within {timeout_seconds}s"
    )


def run_trace(
    *,
    base_url: str,
    token: str,
    run_id: str,
) -> list[dict[str, Any]]:
    return platform_json("GET", base_url, token, f"/v1/streams/{run_id}")


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
