"""绑定地址设置：无法解析的 HOST（conda 的 x86_64-conda-linux-gnu）回落回环；API_HOST 优先。"""

from agent_platform.config import Settings


def test_unresolvable_host_env_falls_back_to_loopback(monkeypatch):
    monkeypatch.setenv("HOST", "x86_64-conda-linux-gnu")
    monkeypatch.delenv("API_HOST", raising=False)
    assert Settings(_env_file=None).host == "127.0.0.1"


def test_api_host_alias_wins_over_generic_host(monkeypatch):
    monkeypatch.setenv("HOST", "x86_64-conda-linux-gnu")
    monkeypatch.setenv("API_HOST", "0.0.0.0")
    monkeypatch.setenv("API_PORT", "8123")
    settings = Settings(_env_file=None)
    assert settings.host == "0.0.0.0"
    assert settings.port == 8123


def test_explicit_loopback_and_port_unchanged(monkeypatch):
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    settings = Settings(_env_file=None, host="localhost", port=9000)
    assert settings.host == "localhost" and settings.port == 9000
