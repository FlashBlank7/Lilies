from __future__ import annotations

import os
import subprocess
from pathlib import Path


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
        "DEEPSEEK_API_KEY=test-key",
        "API_TOKEN=test-token",
        "LILIES_LOCAL_AGENT_ENABLED=true",
    ]
    if explicit_base_url:
        env_lines.append(f"LILIES_PLATFORM_BASE_URL={explicit_base_url}")
    (tmp_path / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    environment = os.environ.copy()
    if not explicit_base_url:
        environment.pop("LILIES_PLATFORM_BASE_URL", None)
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


def test_dev_platform_derives_local_lilies_callback_from_actual_api_port(
    tmp_path: Path,
) -> None:
    output = _check_env(tmp_path, api_host="127.0.0.1", api_port=8123)

    assert "Local Lilies callback http://127.0.0.1:8123" in output


def test_dev_platform_uses_loopback_callback_for_wildcard_bind(
    tmp_path: Path,
) -> None:
    output = _check_env(tmp_path, api_host="0.0.0.0", api_port=8124)

    assert "Local Lilies callback http://127.0.0.1:8124" in output


def test_dev_platform_preserves_explicit_local_lilies_callback(
    tmp_path: Path,
) -> None:
    output = _check_env(
        tmp_path,
        api_host="127.0.0.1",
        api_port=8125,
        explicit_base_url="http://127.0.0.1:9125",
    )

    assert "Local Lilies callback http://127.0.0.1:9125" in output


def test_dev_platform_brackets_ipv6_callback_authority(
    tmp_path: Path,
) -> None:
    output = _check_env(tmp_path, api_host="::1", api_port=8126)

    assert "Local Lilies callback http://[::1]:8126" in output
