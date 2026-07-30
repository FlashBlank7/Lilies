#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
COMPOSE = ROOT / "compose.yaml"
PROJECT = "exp-lilies-002-r1"
DB_VOLUME = "exp-lilies-002-r1-bookstack-db-data"
APP_VOLUME = "exp-lilies-002-r1-bookstack-app-data"
ENV_FILE = Path("/private/tmp/exp-lilies-002-r1.env")
HTTP_BASE = "http://127.0.0.1:18020"


def _write_environment_if_missing() -> None:
    if ENV_FILE.exists():
        return
    app_key = "base64:" + base64.b64encode(os.urandom(32)).decode("ascii")
    payload = (
        f"BOOKSTACK_APP_KEY={app_key}\n"
        f"BOOKSTACK_DB_PASSWORD={secrets.token_urlsafe(32)}\n"
        f"BOOKSTACK_DB_ROOT_PASSWORD={secrets.token_urlsafe(32)}\n"
    )
    ENV_FILE.write_text(payload, encoding="utf-8")
    ENV_FILE.chmod(0o600)


def run(
    *args: str,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        check=check,
        text=True,
        capture_output=capture_output,
    )


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    _write_environment_if_missing()
    return run(
        "docker",
        "compose",
        "--env-file",
        str(ENV_FILE),
        "-f",
        str(COMPOSE),
        "--project-name",
        PROJECT,
        *args,
        check=check,
    )


def volume_exists(name: str) -> bool:
    return (
        run(
            "docker",
            "volume",
            "inspect",
            name,
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


def http_ready() -> bool:
    try:
        with urllib.request.urlopen(f"{HTTP_BASE}/login", timeout=3) as response:
            return response.status == 200
    except (
        urllib.error.URLError,
        TimeoutError,
        http.client.HTTPException,
        OSError,
    ):
        return False


def wait_ready(timeout_seconds: int = 300) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if http_ready():
            return
        time.sleep(2)
    raise TimeoutError("BookStack did not become ready before the timeout")


def ensure() -> dict[str, Any]:
    initialized = volume_exists(DB_VOLUME)
    compose("up", "-d")
    wait_ready()
    return {
        "status": "ready",
        "initialized_now": not initialized,
        "http_base": HTTP_BASE,
        "compose_project": PROJECT,
        "volumes": [DB_VOLUME, APP_VOLUME],
        "secret_file": str(ENV_FILE),
    }


def status() -> dict[str, Any]:
    result = compose("ps", "--format", "json", check=False)
    return {
        "status": "ready" if http_ready() else "not_ready",
        "http_base": HTTP_BASE,
        "database_volume_exists": volume_exists(DB_VOLUME),
        "application_volume_exists": volume_exists(APP_VOLUME),
        "compose_exit_code": result.returncode,
    }


def stop() -> dict[str, Any]:
    compose("stop")
    return {"status": "stopped", "compose_project": PROJECT}


def reset() -> dict[str, Any]:
    expected = {
        "project": "exp-lilies-002-r1",
        "database": "exp-lilies-002-r1-bookstack-db-data",
        "application": "exp-lilies-002-r1-bookstack-app-data",
    }
    actual = {
        "project": PROJECT,
        "database": DB_VOLUME,
        "application": APP_VOLUME,
    }
    if actual != expected:
        raise RuntimeError("refusing to reset an unexpected Docker target")
    compose("down", "--volumes", "--remove-orphans")
    return {
        "status": "reset",
        "removed_volumes": [DB_VOLUME, APP_VOLUME],
        "secret_file_retained": str(ENV_FILE),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("ensure", "status", "stop", "reset"))
    args = parser.parse_args()
    handlers = {
        "ensure": ensure,
        "status": status,
        "stop": stop,
        "reset": reset,
    }
    print(json.dumps(handlers[args.action](), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
