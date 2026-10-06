from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LAUNCH_ENV = (
    "LILIES_STATE_DIR",
    "LILIES_DATA_DIR",
    "LILIES_WORKSPACE_DIR",
    "LILIES_UID",
    "LILIES_GID",
    "LILIES_DOCKER_SOCKET",
    "LILIES_DOCKER_GID",
    "LILIES_API_PORT",
    "LILIES_WEB_PORT",
    "LILIES_BASE_SANDBOX_IMAGE",
    "LILIES_BASE_MODELING_IMAGE",
)


@pytest.fixture
def launcher(tmp_path: Path):
    repo = tmp_path / "source checkout"
    (repo / "scripts").mkdir(parents=True)
    script = repo / "scripts" / "docker-up.sh"
    shutil.copy2(ROOT / "scripts" / "docker-up.sh", script)
    (repo / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    caller = tmp_path / "unrelated working directory"
    caller.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "commands.jsonl"
    socket_directory = tempfile.TemporaryDirectory(prefix="lilies-socket-")
    socket_path = Path(socket_directory.name) / "docker.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as docker_socket:
        docker_socket.bind(str(socket_path))
    stub = f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
event = {{
    "tool": name,
    "args": args,
    "cwd": os.getcwd(),
    "env": {{key: os.environ.get(key) for key in {LAUNCH_ENV!r}}},
}}
with open(os.environ["LAUNCH_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(event) + "\\n")
if name == "id":
    if args == ["-u"]:
        print("2101")
    elif args == ["-g"]:
        print("2102")
    else:
        sys.exit(1)
elif name == "stat":
    print("2103")
elif name == "uname":
    print(os.environ.get("LAUNCH_HOST_OS", "Linux"))
elif name == "docker" and args == ["info"]:
    sys.exit(int(os.environ.get("LAUNCH_DOCKER_INFO_STATUS", "0")))
elif name in ("chmod", "chown"):
    sys.exit(1)
"""
    for name in ("docker", "id", "stat", "uname", "curl", "chmod", "chown"):
        command = fake_bin / name
        command.write_text(stub, encoding="utf-8")
        command.chmod(0o755)

    def run(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None):
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("LILIES_", "SANDBOX_", "DOCKER_", "COMPOSE_"))
        }
        environment.update(
            PATH=str(fake_bin) + os.pathsep + environment.get("PATH", ""),
            LAUNCH_LOG=str(log),
            LILIES_DOCKER_SOCKET=str(socket_path),
            MODEL_EGRESS_ENABLED="false",
        )
        environment.update(env or {})
        log.write_text("", encoding="utf-8")
        result = subprocess.run(
            [str(script), *args],
            cwd=cwd or caller,
            env=environment,
            text=True,
            errors="replace",
            capture_output=True,
            timeout=10,
        )
        events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        return result, events

    try:
        yield repo, caller, socket_path, run
    finally:
        socket_directory.cleanup()


def _compose_calls(events: list[dict]) -> list[dict]:
    return [event for event in events if event["tool"] == "docker"
            and event["args"][:1] == ["compose"]]


def _option(call: dict, option: str) -> str:
    args = call["args"]
    return args[args.index(option) + 1]


@pytest.mark.parametrize(("mode", "operations"), [
    (None, ["up"]),
    ("--build", ["build", "up"]),
    ("--down", ["down"]),
    ("--logs", ["logs"]),
    ("--status", ["ps"]),
])
def test_every_command_uses_the_selected_state_and_launch_user(launcher, mode, operations) -> None:
    repo, _, socket, run = launcher
    state = repo / "restored instance"
    state.mkdir()
    marker = state / "dotenv-was-executed"
    dotenv = state / ".env"
    content = f'API_TOKEN=restored-secret\nMODEL_EGRESS_ENABLED=false\nTEST=$(touch "{marker}")\n'
    dotenv.write_text(content, encoding="utf-8")
    for directory in ("data", "workspaces"):
        (state / directory).mkdir()
        (state / directory / "existing.txt").write_text("restored content", encoding="utf-8")

    result, events = run(
        *([mode] if mode else []),
        env={"LILIES_STATE_DIR": "./restored instance/../restored instance"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = _compose_calls(events)
    assert calls
    assert all(any(operation in call["args"] for call in calls) for operation in operations)
    projects = set()
    for call in calls:
        assert _option(call, "-f") == str(repo / "compose.yaml")
        assert _option(call, "--env-file") == str(dotenv)
        project = _option(call, "--project-name")
        projects.add(project)
        assert call["env"]["LILIES_STATE_DIR"] == str(state)
        assert call["env"]["LILIES_DATA_DIR"] == str(state / "data")
        assert call["env"]["LILIES_WORKSPACE_DIR"] == str(state / "workspaces")
        assert call["env"]["LILIES_UID"] == "2101"
        assert call["env"]["LILIES_GID"] == "2102"
        assert call["env"]["LILIES_DOCKER_SOCKET"] == str(socket)
        assert call["env"]["LILIES_DOCKER_GID"] == "2103"
        assert call["env"]["LILIES_BASE_SANDBOX_IMAGE"] == f"{project}:sandbox"
        assert call["env"]["LILIES_BASE_MODELING_IMAGE"] == f"{project}:modeling"
    assert len(projects) == 1
    assert all(projects)
    assert (state / "data").is_dir()
    assert (state / "workspaces").is_dir()
    for directory in ("data", "workspaces"):
        assert (state / directory / "existing.txt").read_text(encoding="utf-8") == "restored content"
    assert dotenv.read_text(encoding="utf-8") == content
    assert not marker.exists()
    assert "restored-secret" not in result.stdout + result.stderr
    assert any(event["tool"] == "docker" and event["args"] == ["info"] for event in events)
    assert not any(event["tool"] in {"chmod", "chown"} for event in events)


@pytest.mark.parametrize("external_state", [False, True])
def test_start_without_dotenv_does_not_require_a_provider_key(launcher, external_state) -> None:
    repo, caller, _, run = launcher
    state = caller / "empty state" if external_state else repo
    if external_state:
        (repo / ".env").write_text("API_TOKEN=unrelated-source-token\n", encoding="utf-8")

    result, events = run(env={"LILIES_STATE_DIR": str(state)} if external_state else {})

    assert result.returncode == 0, result.stdout + result.stderr
    calls = _compose_calls(events)
    assert calls
    assert all(_option(call, "--env-file") == "/dev/null" for call in calls)
    assert all(call["env"]["LILIES_STATE_DIR"] == str(state) for call in calls)
    assert all(call["env"]["LILIES_API_PORT"] == "8000" for call in calls)
    assert all(call["env"]["LILIES_WEB_PORT"] == "3000" for call in calls)
    assert (state / "data").is_dir()
    assert (state / "workspaces").is_dir()
    assert not (state / ".env").exists()


def test_docker_desktop_uses_container_socket_group_on_macos(launcher) -> None:
    _, _, _, run = launcher

    result, events = run("--status", env={"LAUNCH_HOST_OS": "Darwin"})

    assert result.returncode == 0, result.stdout + result.stderr
    calls = _compose_calls(events)
    assert calls
    assert all(call["env"]["LILIES_DOCKER_GID"] == "0" for call in calls)
    assert not any(event["tool"] == "stat" for event in events)


def test_explicit_socket_group_does_not_use_host_file_group(launcher) -> None:
    _, _, _, run = launcher

    result, events = run("--status", env={"LILIES_DOCKER_GID": "999"})

    assert result.returncode == 0, result.stdout + result.stderr
    calls = _compose_calls(events)
    assert calls
    assert all(call["env"]["LILIES_DOCKER_GID"] == "999" for call in calls)
    assert not any(event["tool"] == "stat" for event in events)


def test_start_waits_for_services_and_displays_selected_ports(launcher) -> None:
    _, _, _, run = launcher

    result, events = run(env={"LILIES_API_PORT": "8123", "LILIES_WEB_PORT": "3123"})

    assert result.returncode == 0, result.stdout + result.stderr
    calls = _compose_calls(events)
    assert calls
    assert all(call["env"]["LILIES_API_PORT"] == "8123" for call in calls)
    assert all(call["env"]["LILIES_WEB_PORT"] == "3123" for call in calls)
    assert "http://127.0.0.1:8123" in result.stdout
    assert "http://127.0.0.1:3123" in result.stdout
    startup = [call for call in calls if "up" in call["args"]]
    assert startup
    assert all("--wait" in call["args"] for call in startup)
    assert all(_option(call, "--wait-timeout") == "60" for call in startup)


def test_compose_project_is_stable_for_state_and_separates_restored_instances(launcher) -> None:
    repo, caller, _, run = launcher
    state = caller / "first" / "restored"
    other_state = caller / "second" / "restored"

    result, first_events = run("--status", env={"LILIES_STATE_DIR": str(state)})
    assert result.returncode == 0, result.stdout + result.stderr
    result, repeated_events = run("--down", env={"LILIES_STATE_DIR": str(state)}, cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    result, other_events = run("--status", env={"LILIES_STATE_DIR": str(other_state)})
    assert result.returncode == 0, result.stdout + result.stderr

    first_project = _option(_compose_calls(first_events)[0], "--project-name")
    assert first_project == _option(_compose_calls(repeated_events)[0], "--project-name")
    assert first_project != _option(_compose_calls(other_events)[0], "--project-name")


def test_unavailable_docker_does_not_start_compose(launcher) -> None:
    _, _, _, run = launcher

    result, events = run("--build", env={"LAUNCH_DOCKER_INFO_STATUS": "1"})

    assert result.returncode != 0
    assert "Docker" in result.stdout + result.stderr
    assert not _compose_calls(events)
