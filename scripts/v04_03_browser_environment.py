#!/usr/bin/env python3
"""Prepare and supervise the isolated v0.4.3 Browser verification environment."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FRONTEND = ROOT / ".tmp/v043-frontend"
DEFAULT_DATA = ROOT / ".tmp/v043-browser/data"
DEFAULT_WORKSPACES = ROOT / ".tmp/v043-browser/workspaces"
DEFAULT_NODE = ROOT / ".tmp/toolchains/node-v22.23.1-darwin-arm64/bin/node"


def prepare_standalone_assets(frontend: Path) -> Path:
    standalone = frontend / ".next/standalone"
    server = standalone / "server.js"
    static = frontend / ".next/static"
    if not server.is_file():
        raise FileNotFoundError(f"standalone server is missing: {server}")
    if not static.is_dir():
        raise FileNotFoundError(f"Next static assets are missing: {static}")
    shutil.copytree(static, standalone / ".next/static", dirs_exist_ok=True)
    public = frontend / "public"
    if public.is_dir():
        shutil.copytree(public, standalone / "public", dirs_exist_ok=True)
    return server


def probe(url: str, *, timeout: float = 2.0) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return {"url": url, "status": response.status, "ready": response.status == 200}
    except (urllib.error.URLError, TimeoutError) as error:
        return {"url": url, "status": 0, "ready": False, "error": str(error)}


def wait_until_ready(urls: list[str], *, timeout: float) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    results = [probe(url) for url in urls]
    while not all(result["ready"] for result in results) and time.monotonic() < deadline:
        time.sleep(0.2)
        results = [probe(url) for url in urls]
    return results


def commands(
    *,
    node: Path,
    standalone_server: Path,
    api_host: str,
    api_port: int,
) -> tuple[list[str], list[str]]:
    backend = [
        str(ROOT / ".venv/bin/python"),
        "-m",
        "uvicorn",
        "agent_platform.api:app",
        "--host",
        api_host,
        "--port",
        str(api_port),
    ]
    frontend = [str(node), str(standalone_server)]
    return backend, frontend


def stop_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def serve(args: argparse.Namespace) -> int:
    frontend_root = args.frontend.resolve()
    standalone_server = prepare_standalone_assets(frontend_root)
    if not args.node.is_file():
        raise FileNotFoundError(f"Node executable is missing: {args.node}")
    backend_command, frontend_command = commands(
        node=args.node,
        standalone_server=standalone_server,
        api_host=args.api_host,
        api_port=args.api_port,
    )
    backend_env = os.environ.copy()
    backend_env.update(
        {
            "DATA_DIR": str(args.data.resolve()),
            "WORKSPACE_ROOT": str(args.workspaces.resolve()),
        }
    )
    frontend_env = os.environ.copy()
    frontend_env.update(
        {
            "HOSTNAME": args.web_host,
            "PORT": str(args.web_port),
            "AGENT_PLATFORM_URL": f"http://{args.api_host}:{args.api_port}",
        }
    )
    args.data.mkdir(parents=True, exist_ok=True)
    args.workspaces.mkdir(parents=True, exist_ok=True)
    backend = subprocess.Popen(backend_command, cwd=ROOT, env=backend_env)
    frontend = subprocess.Popen(
        frontend_command,
        cwd=standalone_server.parent,
        env=frontend_env,
    )
    urls = [
        f"http://{args.api_host}:{args.api_port}/health",
        f"http://{args.web_host}:{args.web_port}/",
    ]
    try:
        health = wait_until_ready(urls, timeout=args.ready_timeout)
        if not all(item["ready"] for item in health):
            print(json.dumps({"ready": False, "health": health}, ensure_ascii=False, indent=2))
            return 1
        print(json.dumps({"ready": True, "health": health}, ensure_ascii=False), flush=True)
        while backend.poll() is None and frontend.poll() is None:
            time.sleep(0.5)
        failed = {
            "backend_exit": backend.poll(),
            "frontend_exit": frontend.poll(),
        }
        print(json.dumps({"ready": False, "process_exit": failed}, ensure_ascii=False))
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        stop_process(frontend)
        stop_process(backend)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands_parser = parser.add_subparsers(dest="command", required=True)
    prepare = commands_parser.add_parser("prepare")
    prepare.add_argument("--frontend", type=Path, default=DEFAULT_FRONTEND)
    check = commands_parser.add_parser("check")
    check.add_argument("--api-url", default="http://127.0.0.1:8002/health")
    check.add_argument("--web-url", default="http://127.0.0.1:3001/")
    server = commands_parser.add_parser("serve")
    server.add_argument("--frontend", type=Path, default=DEFAULT_FRONTEND)
    server.add_argument("--data", type=Path, default=DEFAULT_DATA)
    server.add_argument("--workspaces", type=Path, default=DEFAULT_WORKSPACES)
    server.add_argument("--node", type=Path, default=DEFAULT_NODE)
    server.add_argument("--api-host", default="127.0.0.1")
    server.add_argument("--api-port", type=int, default=8002)
    server.add_argument("--web-host", default="127.0.0.1")
    server.add_argument("--web-port", type=int, default=3001)
    server.add_argument("--ready-timeout", type=float, default=30.0)
    args = parser.parse_args()
    if args.command == "prepare":
        print(prepare_standalone_assets(args.frontend.resolve()))
        return 0
    if args.command == "check":
        results = [probe(args.api_url), probe(args.web_url)]
        print(json.dumps({"ready": all(item["ready"] for item in results), "health": results}))
        return 0 if all(item["ready"] for item in results) else 1
    return serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
