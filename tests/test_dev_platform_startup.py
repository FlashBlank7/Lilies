from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _check_env(
    tmp_path: Path,
    *,
    api_host: str,
    api_port: int,
    explicit_base_url: str = "",
) -> str:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "dev_platform.sh"
    script.write_text(
        (ROOT / "scripts" / "dev_platform.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    env_lines = [
        "API_TOKEN=test-token",
        "MODEL_EGRESS_ENABLED=false",
    ]
    if explicit_base_url:
        env_lines.append(f"STUDIO_PLATFORM_URL={explicit_base_url}")
    (tmp_path / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    environment = os.environ.copy()
    if not explicit_base_url:
        environment.pop("STUDIO_PLATFORM_URL", None)
    environment.update({"API_HOST": api_host, "API_PORT": str(api_port)})
    result = subprocess.run(
        ["bash", str(script), "--check-env"],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_dev_platform_derives_studio_target_from_actual_api_port(
    tmp_path: Path,
) -> None:
    output = _check_env(tmp_path, api_host="127.0.0.1", api_port=8123)

    assert "Studio proxy target: http://127.0.0.1:8123" in output


def test_dev_platform_uses_loopback_target_for_wildcard_bind(
    tmp_path: Path,
) -> None:
    output = _check_env(tmp_path, api_host="0.0.0.0", api_port=8124)

    assert "Studio proxy target: http://127.0.0.1:8124" in output


def test_dev_platform_preserves_explicit_studio_target(
    tmp_path: Path,
) -> None:
    output = _check_env(
        tmp_path,
        api_host="127.0.0.1",
        api_port=8125,
        explicit_base_url="http://127.0.0.1:9125",
    )

    assert "Studio proxy target: http://127.0.0.1:9125" in output


def test_dev_platform_brackets_ipv6_target_authority(
    tmp_path: Path,
) -> None:
    output = _check_env(tmp_path, api_host="::1", api_port=8126)

    assert "Studio proxy target: http://[::1]:8126" in output


@pytest.mark.parametrize('with_token', [True, False])
def test_local_start_does_not_require_removed_bridges_or_provider_key(tmp_path: Path, with_token: bool) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "dev_platform.sh"
    script.write_text(
        (ROOT / "scripts" / "dev_platform.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                "API_TOKEN=test-token" if with_token else "API_TOKEN=",
                "MODEL_EGRESS_ENABLED=false",
                "LILIES_COLLABORATION_ENABLED=true",
                "LILIES_COLLABORATIVE_DEVELOPMENT_ENABLED=true",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    (tmp_path / '.venv/bin').mkdir(parents=True)
    (tmp_path / 'platform/frontend/node_modules').mkdir(parents=True)
    commands = {
        fake_bin / 'npm': '#!/bin/sh\nprintf "web:%s\\n" "$AGENT_PLATFORM_URL" >> "$LAUNCH_LOG"\n',
        fake_bin / 'docker': '#!/bin/sh\nexit 0\n',
        fake_bin / 'lsof': '#!/bin/sh\nexit 1\n',
        tmp_path / '.venv/bin/uvicorn': '#!/bin/sh\nprintf "api:%s\\n" "$*" >> "$LAUNCH_LOG"\n',
    }
    for path, content in commands.items():
        path.write_text(content)
        path.chmod(0o755)
    log = tmp_path / 'launched.txt'
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'DEEPSEEK_API_KEY', 'STUDIO_PLATFORM_URL'}}
    environment.update(PATH=str(fake_bin) + os.pathsep + environment.get('PATH', ''),
                       API_HOST='127.0.0.1', API_PORT='8123', LAUNCH_LOG=str(log))
    result = subprocess.run(['bash', str(script)], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=10)
    assert 'test-token' not in result.stdout + result.stderr
    assert result.returncode == 0, result.stderr
    assert 'web:http://127.0.0.1:8123' in log.read_text()
    assert 'api:agent_platform.api:app --host 127.0.0.1 --port 8123' in log.read_text()


def test_example_configuration_keeps_provider_egress_closed(tmp_path: Path, monkeypatch) -> None:
    from agent_platform.config import Settings
    monkeypatch.delenv('MODEL_EGRESS_ENABLED', raising=False)
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    settings = Settings(_env_file=ROOT / '.env.example', data_dir=tmp_path / 'data',
                        workspace_root=tmp_path / 'workspaces')
    assert settings.model_egress_enabled is False
    assert not settings.deepseek_api_key
    assert settings.modeling_image == 'lilies-modeling:20260914'
