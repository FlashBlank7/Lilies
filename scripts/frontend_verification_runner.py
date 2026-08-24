#!/usr/bin/env python3
"""Run frontend verification with a discovered Node/npm environment."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "platform" / "frontend"


def candidate_node_bins(home: Path | None = None) -> list[Path]:
    resolved_home = home or Path.home()
    candidates = []
    path_node = shutil.which("node")
    if path_node:
        candidates.append(Path(path_node).resolve().parent)
    candidates.extend(
        Path(path)
        for path in sorted(glob.glob(str(resolved_home / ".nvm" / "versions" / "node" / "*" / "bin")), reverse=True)
    )
    candidates.append(resolved_home / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node" / "bin")
    # 无 root 的服务器上（如 bagpipe）Node 直接解到 ~/.local/node——
    # 不收录这条，前端验证会在没有 PATH 加持时误报"环境不可用"。
    candidates.append(resolved_home / ".local" / "node" / "bin")
    seen = set()
    unique = []
    for candidate in candidates:
        normalized = candidate.expanduser()
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def probe_node_environment(frontend_dir: Path = FRONTEND_DIR) -> dict[str, Any]:
    candidates = []
    selected = None
    for node_bin in candidate_node_bins():
        node_path = node_bin / "node"
        npm_path = node_bin / "npm"
        usable = node_path.exists() and npm_path.exists()
        candidates.append({
            "node_bin": node_bin.as_posix(),
            "node_exists": node_path.exists(),
            "npm_exists": npm_path.exists(),
            "usable": usable,
        })
        if usable and selected is None:
            selected = node_bin
    return {
        "frontend_dir": frontend_dir.relative_to(ROOT).as_posix(),
        "package_json_present": (frontend_dir / "package.json").exists(),
        "package_lock_present": (frontend_dir / "package-lock.json").exists(),
        "node_modules_present": (frontend_dir / "node_modules").exists(),
        "selected_node_bin": selected.as_posix() if selected else None,
        "node_available": selected is not None,
        "npm_available": selected is not None,
        "candidates": candidates,
    }


def repaired_env(selected_node_bin: str) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{selected_node_bin}{os.pathsep}{env.get('PATH', '')}"
    return env


def run_command(command: list[str], *, cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    completed = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=False)
    return {
        "command": " ".join(command),
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
        "passed": completed.returncode == 0,
    }


def run_frontend_verification(frontend_dir: Path = FRONTEND_DIR) -> dict[str, Any]:
    probe = probe_node_environment(frontend_dir)
    if not probe["selected_node_bin"]:
        return {
            "status": "blocked",
            "reason": "no usable node/npm pair found",
            "probe": probe,
            "checks": [],
            "passed": False,
        }
    env = repaired_env(probe["selected_node_bin"])
    checks = [
        run_command(["npm", "run", "lint"], cwd=frontend_dir, env=env),
        run_command(["node_modules/.bin/tsc", "--noEmit"], cwd=frontend_dir, env=env),
    ]
    passed = all(check["passed"] for check in checks)
    return {
        "status": "completed" if passed else "failed",
        "reason": "frontend verification passed" if passed else "one or more frontend verification checks failed",
        "probe": probe,
        "checks": checks,
        "passed": passed,
    }


def main() -> None:
    result = run_frontend_verification()
    for check in result["checks"]:
        print(f"{check['command']}: {check['returncode']}")
    print(result["status"])
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
