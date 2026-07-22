from __future__ import annotations

import sys
import json
from argparse import Namespace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import httpx

from agent_platform import lilies_cli
from agent_platform.lilies_client import (
    CLI_SCOPES,
    PLATFORM_PAIRING_SCOPES,
    LiliesClient,
    LiliesClientError,
)
from agent_platform.lilies_config import LiliesSettings
from agent_platform.lilies_daemon import read_daemon_info, write_daemon_info


def prepared_settings(tmp_path: Path, **values: Any) -> LiliesSettings:
    settings = LiliesSettings(data_dir=tmp_path / "lilies", **values)
    settings.prepare()
    return settings


def test_parser_exposes_the_standalone_command_surface() -> None:
    parser = lilies_cli.build_parser()

    assert parser.parse_args(["serve"]).command == "serve"
    assert parser.parse_args(["chat", "hello", "Lilies"]).prompt == ["hello", "Lilies"]
    assert parser.parse_args(["chat", "--session", "session-1"]).session == "session-1"
    assert parser.parse_args(["sessions"]).command == "sessions"
    assert parser.parse_args(["attach", "session-1"]).session_id == "session-1"
    assert parser.parse_args(["status"]).command == "status"
    assert parser.parse_args(["pair"]).command == "pair"
    assert parser.parse_args(
        [
            "pair",
            "--scope",
            "lilies.session:read",
            "--scope",
            "lilies.credential:write",
        ]
    ).scopes == ["lilies.session:read", "lilies.credential:write"]
    with pytest.raises(SystemExit):
        parser.parse_args(["pair", "--scope", "unknown.scope"])
    assert parser.parse_args(["stop"]).command == "stop"


def test_serve_refuses_non_loopback_without_explicit_ack(tmp_path: Path) -> None:
    settings = prepared_settings(tmp_path, host="0.0.0.0")
    args = Namespace(allow_non_loopback=False, log_level="info")

    with pytest.raises(LiliesClientError, match="refusing a non-loopback bind"):
        lilies_cli._run_serve(settings, args)

    assert not settings.daemon_file.exists()


def test_serve_writes_private_record_injects_stop_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = prepared_settings(tmp_path)
    app = SimpleNamespace(state=SimpleNamespace())
    api_module = ModuleType("agent_platform.lilies_api")
    api_module.create_lilies_app = lambda received: app  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent_platform.lilies_api", api_module)

    observed: dict[str, Any] = {}

    class FakeServer:
        def __init__(self, config: Any) -> None:
            observed["config"] = config
            self.should_exit = False

        def run(self) -> None:
            observed["record"] = read_daemon_info(settings)
            observed["mode"] = settings.daemon_file.stat().st_mode & 0o777
            app.state.request_daemon_stop()
            observed["should_exit"] = self.should_exit

    monkeypatch.setattr(lilies_cli.uvicorn, "Server", FakeServer)

    assert lilies_cli._run_serve(
        settings,
        Namespace(allow_non_loopback=False, log_level="warning"),
    ) == 0
    assert observed["record"]["address"] == "http://127.0.0.1:8765"
    assert observed["mode"] == 0o600
    assert observed["should_exit"] is True
    assert not settings.daemon_file.exists()


def test_chat_missing_named_session_never_creates_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = prepared_settings(tmp_path)

    class FakeClient:
        created = False

        def get_session(self, session_id: str) -> dict[str, Any]:
            raise LiliesClientError("status 404")

        def create_session(self) -> dict[str, Any]:
            self.created = True
            return {"session_id": "unexpected"}

    client = FakeClient()
    monkeypatch.setattr(lilies_cli, "_connect_for_chat", lambda *args, **kwargs: client)

    with pytest.raises(LiliesClientError, match="no session was created"):
        lilies_cli._run_chat(
            settings,
            Namespace(session="missing", no_start=True, start_timeout=1.0, prompt=[]),
        )

    assert client.created is False


def test_chat_no_start_reports_missing_daemon_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = prepared_settings(tmp_path)
    started = False

    class OfflineClient:
        def health(self) -> dict[str, Any]:
            raise FileNotFoundError

    def fail_if_started(*args: Any, **kwargs: Any) -> None:
        nonlocal started
        started = True

    monkeypatch.setattr(lilies_cli, "_client_for", lambda value: OfflineClient())
    monkeypatch.setattr(lilies_cli, "_start_background_daemon", fail_if_started)

    with pytest.raises(LiliesClientError, match="--no-start was specified"):
        lilies_cli._connect_for_chat(settings, no_start=True, start_timeout=1.0)
    assert started is False


def test_chat_starts_a_missing_daemon_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = prepared_settings(tmp_path)
    online = False

    class EventuallyOnlineClient:
        def health(self) -> dict[str, Any]:
            if not online:
                raise FileNotFoundError
            return {"schema_version": "1.0"}

    client = EventuallyOnlineClient()

    def start(value: LiliesSettings, *, timeout: float) -> None:
        nonlocal online
        assert value is settings
        assert timeout == 2.0
        online = True

    monkeypatch.setattr(lilies_cli, "_client_for", lambda value: client)
    monkeypatch.setattr(lilies_cli, "_start_background_daemon", start)

    assert lilies_cli._connect_for_chat(settings, no_start=False, start_timeout=2.0) is client


def test_background_start_uses_detached_loopback_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = prepared_settings(tmp_path, host="0.0.0.0", port=9876)
    observed: dict[str, Any] = {}

    class HealthyClient:
        def health(self) -> dict[str, Any]:
            return {"status": "ok"}

    class FakeProcess:
        def poll(self) -> None:
            return None

    def popen(command: list[str], **kwargs: Any) -> FakeProcess:
        observed["command"] = command
        observed["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(lilies_cli, "_client_for", lambda value: HealthyClient())
    monkeypatch.setattr(lilies_cli.subprocess, "Popen", popen)

    lilies_cli._start_background_daemon(settings, timeout=1.0)

    command = observed["command"]
    assert command[-5:] == ["serve", "--host", "127.0.0.1", "--port", "9876"]
    assert observed["kwargs"]["start_new_session"] is True
    assert observed["kwargs"]["stdin"] is lilies_cli.subprocess.DEVNULL


def test_client_creates_unexchanged_pairing_code_with_exact_scopes(tmp_path: Path) -> None:
    settings = prepared_settings(tmp_path)
    daemon = write_daemon_info(settings)
    requested_scopes = ["lilies.session:read", "lilies.credential:write"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/local/v1/pairings/code"
        assert "authorization" not in request.headers
        assert json.loads(request.content) == {
            "allowed_scopes": requested_scopes,
            "ttl_seconds": 600,
        }
        return httpx.Response(
            200,
            request=request,
            json={
                "pairing_code": "ABCD-EFGH",
                "allowed_scopes": requested_scopes,
                "expires_at": "2026-07-22T12:00:00+00:00",
                "daemon_fingerprint": daemon["daemon_fingerprint"],
            },
        )

    client = LiliesClient(settings, transport=httpx.MockTransport(handler))

    result = client.create_pairing_code(requested_scopes)

    assert result["pairing_code"] == "ABCD-EFGH"
    assert not client.token_file.exists()


def test_pairing_code_client_rejects_unknown_or_duplicate_scope_before_http(tmp_path: Path) -> None:
    settings = prepared_settings(tmp_path)
    write_daemon_info(settings)

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP request: {request.url}")

    client = LiliesClient(settings, transport=httpx.MockTransport(unexpected_request))

    with pytest.raises(LiliesClientError, match="unknown local pairing scope"):
        client.create_pairing_code(["unknown.scope"])
    with pytest.raises(LiliesClientError, match="must not contain duplicates"):
        client.create_pairing_code(["lilies.session:read", "lilies.session:read"])


def test_client_force_pairing_sends_expired_identity_proof_and_preserves_client_id(
    tmp_path: Path,
) -> None:
    settings = prepared_settings(tmp_path)
    daemon = write_daemon_info(settings)
    client_id = "11111111-1111-4111-8111-111111111111"
    old_token = f"{client_id}." + "old-token-proof-" * 3
    new_token = f"{client_id}." + "new-token-value-" * 3
    client_name = "cli:rotation-test"
    observed_exchange: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/local/v1/pairings/code":
            return httpx.Response(
                200,
                request=request,
                json={
                    "pairing_code": "ABCD-EFGH",
                    "allowed_scopes": list(CLI_SCOPES),
                    "expires_at": "2026-07-22T12:00:00+00:00",
                    "daemon_fingerprint": daemon["daemon_fingerprint"],
                },
            )
        assert request.url.path == "/local/v1/pairings/exchange"
        observed_exchange.update(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "client_id": client_id,
                "access_token": new_token,
                "granted_scopes": list(CLI_SCOPES),
                "expires_at": "2099-07-23T00:00:00+00:00",
                "daemon_fingerprint": daemon["daemon_fingerprint"],
            },
        )

    client = LiliesClient(settings, transport=httpx.MockTransport(handler))
    client._save_token_record(
        {
            "client_id": client_id,
            "client_name": client_name,
            "access_token": old_token,
            "granted_scopes": list(CLI_SCOPES),
            "expires_at": "2000-01-01T00:00:00+00:00",
            "daemon_fingerprint": daemon["daemon_fingerprint"],
            "address": daemon["address"],
        }
    )

    rotated = client.ensure_pairing(force=True)

    assert observed_exchange["client_name"] == client_name
    assert observed_exchange["previous_client_id"] == client_id
    assert observed_exchange["previous_access_token"] == old_token
    assert rotated["client_id"] == client_id
    assert rotated["access_token"] == new_token
    persisted = json.loads(client.token_file.read_text(encoding="utf-8"))
    assert persisted["access_token"] == new_token
    assert old_token not in client.token_file.read_text(encoding="utf-8")
    assert client.token_file.stat().st_mode & 0o777 == 0o600


def test_pair_output_shows_one_time_code_and_fingerprint_but_never_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert PLATFORM_PAIRING_SCOPES == [
        "lilies.session:read",
        "lilies.session:write",
        "lilies.credential:write",
    ]
    settings = prepared_settings(tmp_path)
    fingerprint = settings.daemon_fingerprint()

    write_daemon_info(settings)

    class FakeClient:
        def health(self) -> dict[str, Any]:
            return {"schema_version": "1.0"}

        def create_pairing_code(self, allowed_scopes: list[str]) -> dict[str, Any]:
            assert allowed_scopes == PLATFORM_PAIRING_SCOPES
            return {
                "pairing_code": "JKLM-NPQR",
                "daemon_fingerprint": fingerprint,
                "allowed_scopes": PLATFORM_PAIRING_SCOPES,
                "expires_at": "2026-07-23T00:00:00+00:00",
            }

    monkeypatch.setattr(lilies_cli, "_client_for", lambda value: FakeClient())
    args = Namespace(command="pair", scopes=None)

    assert lilies_cli._dispatch(args, settings) == 0
    output = capsys.readouterr().out
    assert "JKLM-NPQR" in output
    assert fingerprint in output
    assert "lilies.daemon:control" not in output
    assert "lilies.permission:resolve" not in output
    assert "access_token" not in output


def test_status_and_session_header_use_explicit_platform_pairing_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert "platform: connected" in lilies_cli._status_line(
        {"model": "test", "platform_paired": True, "active_session_count": 0}
    )
    assert "platform: disconnected" in lilies_cli._status_line(
        {"model": "test", "platform_paired": False, "active_session_count": 0}
    )

    class FakeClient:
        def status(self) -> dict[str, Any]:
            return {"model": "test", "platform_paired": True}

    lilies_cli._print_session_header(
        FakeClient(),  # type: ignore[arg-type]
        {"session_id": "session-12345678", "status": "ready"},
    )
    assert "platform: connected" in capsys.readouterr().out


def test_permission_event_prompts_and_resolves_using_redacted_details(
    capsys: pytest.CaptureFixture[str],
) -> None:
    decisions: list[tuple[str, str, str, str]] = []

    class FakeClient:
        def resolve_permission(
            self,
            session_id: str,
            request_id: str,
            *,
            behavior: str,
            expected_input_digest: str,
        ) -> dict[str, Any]:
            decisions.append((session_id, request_id, behavior, expected_input_digest))
            return {}

    lilies_cli._render_event(
        FakeClient(),  # type: ignore[arg-type]
        "session-1",
        {
            "id": 9,
            "type": "permission.requested",
            "data": {
                "request_id": "permission-1",
                "tool_name": "workspace_write",
                "input_digest": "sha256:" + "a" * 64,
                "input_summary": {
                    "path": "notes/result.txt",
                    "access_token": "must-not-print",
                },
            },
        },
        input_fn=lambda prompt: "deny",
    )

    output = capsys.readouterr().out
    assert decisions == [("session-1", "permission-1", "deny", "sha256:" + "a" * 64)]
    assert "workspace_write" in output
    assert "must-not-print" not in output
    assert "[redacted]" in output


def test_received_events_are_acked_and_inspect_cache_is_redacted() -> None:
    acknowledgements: list[int] = []

    class FakeClient:
        def iter_events(self, session_id: str, *, after: int):
            assert (session_id, after) == ("session-1", 3)
            yield {
                "id": 4,
                "type": "model.text.delta",
                "data": {"text": "done", "authorization": "Bearer private"},
            }
            yield {
                "id": 5,
                "type": "turn.finished",
                "data": {"status": "completed"},
            }

        def ack(self, session_id: str, cursor: int) -> dict[str, Any]:
            assert session_id == "session-1"
            acknowledgements.append(cursor)
            return {}

    cache: dict[int, dict[str, Any]] = {}
    cursor = lilies_cli._consume_turn_events(
        FakeClient(),  # type: ignore[arg-type]
        "session-1",
        after=3,
        event_cache=cache,
        input_fn=lambda prompt: "deny",
    )

    assert cursor == 5
    assert acknowledgements == [4, 5]
    assert cache[4]["data"]["authorization"] == "[redacted]"
