#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.client
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
COMPOSE = ROOT / "compose.yaml"
PROJECT = "exp-lilies-004-r1"
POSTGRES_VOLUME = "exp-lilies-004-r1-postgres-data"
HTTP_BASE = "http://127.0.0.1:19090"


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
    return run(
        "docker",
        "compose",
        "-f",
        str(COMPOSE),
        "--project-name",
        PROJECT,
        *args,
        check=check,
    )


def volume_exists() -> bool:
    result = run(
        "docker",
        "volume",
        "inspect",
        POSTGRES_VOLUME,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


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
    raise TimeoutError("ThingsBoard did not become ready before the timeout")


def ensure() -> dict[str, Any]:
    initialized = volume_exists()
    if not initialized:
        compose("up", "-d", "postgres")
        compose(
            "run",
            "--rm",
            "-e",
            "INSTALL_TB=true",
            "-e",
            "LOAD_DEMO=true",
            "thingsboard-ce",
        )
    compose("up", "-d")
    wait_ready()
    return {
        "status": "ready",
        "initialized_now": not initialized,
        "http_base": HTTP_BASE,
        "mqtt_host": "127.0.0.1",
        "mqtt_port": 18884,
        "compose_project": PROJECT,
        "postgres_volume": POSTGRES_VOLUME,
    }


def status() -> dict[str, Any]:
    result = compose("ps", "--format", "json", check=False)
    return {
        "status": "ready" if http_ready() else "not_ready",
        "http_base": HTTP_BASE,
        "volume_exists": volume_exists(),
        "compose_exit_code": result.returncode,
    }


def stop() -> dict[str, Any]:
    compose("stop")
    return {"status": "stopped", "compose_project": PROJECT}


def reset() -> dict[str, Any]:
    if PROJECT != "exp-lilies-004-r1" or POSTGRES_VOLUME != (
        "exp-lilies-004-r1-postgres-data"
    ):
        raise RuntimeError("refusing to reset an unexpected Docker target")
    compose("down", "--volumes", "--remove-orphans")
    return {"status": "reset", "removed_volume": POSTGRES_VOLUME}


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
