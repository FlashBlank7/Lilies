from __future__ import annotations

import json
import os
import secrets
import socket
import tempfile
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from .lilies_config import LiliesSettings
from .lilies_daemon import read_daemon_info
from .lilies_models import LocalScope


CLI_TOKEN_FILE = "cli-client.json"
CLI_SCOPES = [
    "lilies.session:read",
    "lilies.session:write",
    "lilies.permission:resolve",
    "lilies.daemon:control",
    "lilies.credential:write",
]
ALL_LOCAL_SCOPES = frozenset(scope.value for scope in LocalScope)
PLATFORM_PAIRING_SCOPES = [
    LocalScope.session_read.value,
    LocalScope.session_write.value,
    LocalScope.credential_write.value,
]


class LiliesClientError(RuntimeError):
    pass


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class LiliesClient:
    def __init__(
        self,
        settings: LiliesSettings,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.timeout = timeout
        self.transport = transport
        self.token_file = settings.data_dir / CLI_TOKEN_FILE

    def health(self) -> dict[str, Any]:
        daemon = read_daemon_info(self.settings)
        return self._request_json("GET", "/local/v1/health", auth=False, base_url=daemon["address"])

    def create_pairing_code(self, allowed_scopes: Sequence[str]) -> dict[str, Any]:
        """Create an unexchanged ten-minute code for a separate local client."""

        scopes = list(allowed_scopes)
        if not scopes:
            raise LiliesClientError("a pairing code requires at least one allowed scope")
        unknown = sorted(set(scopes) - ALL_LOCAL_SCOPES)
        if unknown:
            raise LiliesClientError(f"unknown local pairing scope: {', '.join(unknown)}")
        if len(scopes) != len(set(scopes)):
            raise LiliesClientError("pairing scopes must not contain duplicates")
        daemon = read_daemon_info(self.settings)
        result = self._request_json(
            "POST",
            "/local/v1/pairings/code",
            auth=False,
            base_url=daemon["address"],
            json_body={"allowed_scopes": scopes, "ttl_seconds": 600},
        )
        if not isinstance(result, dict):
            raise LiliesClientError("daemon returned an invalid pairing code response")
        returned_scopes = result.get("allowed_scopes")
        if (
            not isinstance(result.get("pairing_code"), str)
            or not isinstance(result.get("expires_at"), str)
            or not isinstance(returned_scopes, list)
            or not returned_scopes
            or any(scope not in ALL_LOCAL_SCOPES for scope in returned_scopes)
            or returned_scopes != scopes
            or "access_token" in result
        ):
            raise LiliesClientError("daemon returned an invalid pairing code response")
        if result.get("daemon_fingerprint") != daemon["daemon_fingerprint"]:
            raise LiliesClientError("daemon fingerprint changed while creating pairing code")
        return result

    def ensure_pairing(self, *, force: bool = False) -> dict[str, Any]:
        daemon = read_daemon_info(self.settings)
        existing = self._load_token_record(required=False)
        if not force and existing and self._token_record_is_valid(existing, daemon):
            return existing
        previous = (
            existing
            if (
                existing is not None
                and self._token_record_belongs_to_daemon(existing, daemon)
                and isinstance(existing.get("client_id"), str)
                and isinstance(existing.get("access_token"), str)
            )
            else None
        )
        default_client_name = f"cli:{socket.gethostname()[:116]}"
        client_name = (
            str(previous.get("client_name"))
            if previous is not None and isinstance(previous.get("client_name"), str)
            else default_client_name
        )
        code = self.create_pairing_code(CLI_SCOPES)
        exchange_body: dict[str, Any] = {
            "pairing_code": code["pairing_code"],
            "client_name": client_name,
            "requested_scopes": CLI_SCOPES,
            "client_nonce": secrets.token_urlsafe(24),
        }
        if previous is not None:
            exchange_body.update(
                {
                    "previous_client_id": previous["client_id"],
                    "previous_access_token": previous["access_token"],
                }
            )
        exchanged = self._request_json(
            "POST",
            "/local/v1/pairings/exchange",
            auth=False,
            base_url=daemon["address"],
            json_body=exchange_body,
        )
        if exchanged.get("daemon_fingerprint") != daemon["daemon_fingerprint"]:
            raise LiliesClientError("daemon fingerprint changed during pairing")
        if previous is not None and exchanged.get("client_id") != previous.get("client_id"):
            raise LiliesClientError("daemon did not preserve the proven local client identity")
        record = {
            "client_id": exchanged["client_id"],
            "access_token": exchanged["access_token"],
            "granted_scopes": exchanged["granted_scopes"],
            "expires_at": exchanged.get("expires_at"),
            "daemon_fingerprint": exchanged["daemon_fingerprint"],
            "address": daemon["address"],
            "client_name": client_name,
        }
        self._save_token_record(record)
        return record

    def status(self) -> dict[str, Any]:
        return self.request("GET", "/local/v1/status")

    def sessions(self) -> list[dict[str, Any]]:
        result = self.request("GET", "/local/v1/sessions")
        return list(result.get("sessions", result if isinstance(result, list) else []))

    def create_session(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = dict(payload or {})
        body.setdefault("schema_version", "1.0")
        body.setdefault("idempotency_key", self._idempotency_key("session"))
        return self.request("POST", "/local/v1/sessions", json_body=body)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self.request("GET", f"/local/v1/sessions/{session_id}")

    def send_message(
        self,
        session_id: str,
        content: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/local/v1/sessions/{session_id}/messages",
            json_body={
                "content": content,
                "message_id": str(uuid4()),
                "idempotency_key": idempotency_key or self._idempotency_key("message"),
            },
        )

    def resume(self, session_id: str) -> dict[str, Any]:
        status = self.get_session(session_id)["status"]
        return self.request(
            "POST",
            f"/local/v1/sessions/{session_id}/resume",
            json_body={
                "idempotency_key": self._idempotency_key("resume"),
                "expected_status": status,
                "reason": "explicit CLI resume",
            },
        )

    def cancel(self, session_id: str, *, reason: str = "user requested cancellation") -> dict[str, Any]:
        return self.request(
            "POST",
            f"/local/v1/sessions/{session_id}/cancel",
            json_body={
                "idempotency_key": self._idempotency_key("cancel"),
                "reason": reason,
            },
        )

    def resolve_permission(
        self,
        session_id: str,
        request_id: str,
        *,
        behavior: str,
        expected_input_digest: str,
        message: str | None = None,
        updated_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/local/v1/sessions/{session_id}/permissions/{request_id}",
            json_body={
                "idempotency_key": self._idempotency_key("permission"),
                "behavior": behavior,
                "expected_input_digest": expected_input_digest,
                "updated_input": updated_input,
                "message": message,
            },
        )

    def ack(self, session_id: str, cursor: int) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/local/v1/sessions/{session_id}/acks",
            json_body={
                "idempotency_key": self._idempotency_key("ack"),
                "cursor": cursor,
            },
        )

    def stop(self, *, reason: str = "CLI stop") -> dict[str, Any]:
        return self.request(
            "POST",
            "/local/v1/control/stop",
            json_body={
                "idempotency_key": self._idempotency_key("stop"),
                "reason": reason,
                "cancel_active_turns": True,
                "grace_period_seconds": 10,
            },
        )

    def iter_events(self, session_id: str, *, after: int = 0) -> Iterator[dict[str, Any]]:
        record = self.ensure_pairing()
        headers = {
            "authorization": f"Bearer {record['access_token']}",
            "accept": "text/event-stream",
            "last-event-id": str(after),
        }
        event: dict[str, Any] = {}
        try:
            with httpx.Client(
                base_url=record["address"],
                timeout=None,
                transport=self.transport,
            ) as client:
                with client.stream(
                    "GET", f"/local/v1/sessions/{session_id}/events", headers=headers
                ) as response:
                    self._raise_for_status(response)
                    for line in response.iter_lines():
                        if not line:
                            if "data" in event:
                                yield event
                            event = {}
                            continue
                        field, separator, value = line.partition(":")
                        if not separator:
                            continue
                        value = value.lstrip()
                        if field == "id":
                            event["id"] = int(value)
                        elif field == "event":
                            event["type"] = value
                        elif field == "data":
                            event["data"] = json.loads(value)
        except httpx.HTTPError as error:
            raise LiliesClientError(f"daemon event stream failed: {type(error).__name__}") from error

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        record = self.ensure_pairing()
        try:
            return self._request_json(
                method,
                path,
                auth=True,
                base_url=record["address"],
                json_body=json_body,
                access_token=record["access_token"],
            )
        except LiliesClientError as error:
            if "status 401" not in str(error):
                raise
        record = self.ensure_pairing(force=True)
        return self._request_json(
            method,
            path,
            auth=True,
            base_url=record["address"],
            json_body=json_body,
            access_token=record["access_token"],
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        auth: bool,
        base_url: str,
        json_body: dict[str, Any] | None = None,
        access_token: str | None = None,
    ) -> Any:
        headers: dict[str, str] = {"accept": "application/json"}
        if auth:
            if not access_token:
                raise LiliesClientError("local client is not paired")
            headers["authorization"] = f"Bearer {access_token}"
        try:
            with httpx.Client(
                base_url=base_url,
                timeout=self.timeout,
                transport=self.transport,
            ) as client:
                response = client.request(method, path, json=json_body, headers=headers)
            self._raise_for_status(response)
            return response.json()
        except httpx.HTTPError as error:
            raise LiliesClientError(f"daemon request failed: {type(error).__name__}") from error

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        detail = "request rejected"
        try:
            payload = response.json()
            candidate = payload.get("detail") if isinstance(payload, dict) else None
            if isinstance(candidate, str):
                detail = candidate[:500]
            elif isinstance(candidate, dict):
                detail = str(candidate.get("code", "request rejected"))[:500]
        except (json.JSONDecodeError, ValueError):
            pass
        raise LiliesClientError(f"daemon returned status {response.status_code}: {detail}")

    def _load_token_record(self, *, required: bool) -> dict[str, Any] | None:
        try:
            mode = self.token_file.stat().st_mode & 0o777
            if mode != 0o600:
                raise PermissionError(f"CLI token file must have mode 0600, found {mode:04o}")
            value = json.loads(self.token_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if required:
                raise LiliesClientError("local client is not paired") from None
            return None
        if not isinstance(value, dict) or not isinstance(value.get("access_token"), str):
            raise LiliesClientError("invalid CLI token record")
        return value

    @staticmethod
    def _token_record_is_valid(record: dict[str, Any], daemon: dict[str, Any]) -> bool:
        if not LiliesClient._token_record_belongs_to_daemon(record, daemon):
            return False
        expires_at = _parse_datetime(record.get("expires_at"))
        return expires_at is None or expires_at > datetime.now(timezone.utc)

    @staticmethod
    def _token_record_belongs_to_daemon(
        record: dict[str, Any], daemon: dict[str, Any]
    ) -> bool:
        return (
            record.get("daemon_fingerprint") == daemon.get("daemon_fingerprint")
            and record.get("address") == daemon.get("address")
        )

    def _save_token_record(self, record: dict[str, Any]) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".cli-client.", dir=self.settings.data_dir)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.token_file)
            os.chmod(self.token_file, 0o600)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    @staticmethod
    def _idempotency_key(prefix: str) -> str:
        return f"{prefix}:{uuid4().hex}"
