from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID

import httpx

from .collaboration_models import (
    DeveloperInboxResponse,
    DeveloperLease,
    DeveloperResponse,
    DeveloperResponsePayload,
)


_SENSITIVE_TEXT = re.compile(
    r"(?i)\b(?:authorization|cookie|password|secret|token|api[_-]?key)\b"
    r"\s*[:=]\s*[^\s,;]+"
)


class DeveloperCollaborationClientError(RuntimeError):
    """A redacted failure at the developer collaboration HTTP boundary."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def normalize_platform_base_url(value: str) -> str:
    raw = value.strip()
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise DeveloperCollaborationClientError(
            "platform base URL must be an http(s) URL without credentials, query, or fragment"
        )
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def read_developer_token(*, environment_value: str, token_file: Path | None) -> str:
    if token_file is not None:
        if token_file.is_symlink():
            raise DeveloperCollaborationClientError(
                "developer token file must not be a symlink"
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(token_file, flags)
        except OSError as error:
            raise DeveloperCollaborationClientError(
                "developer token file is not readable"
            ) from error
        try:
            with os.fdopen(descriptor, encoding="utf-8") as handle:
                metadata = os.fstat(handle.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise DeveloperCollaborationClientError(
                        "developer token file must be a regular file"
                    )
                if metadata.st_mode & 0o777 != 0o600:
                    raise DeveloperCollaborationClientError(
                        "developer token file must have mode 0600"
                    )
                token = handle.read(4_097).strip()
        except (OSError, UnicodeError) as error:
            raise DeveloperCollaborationClientError(
                "developer token file is not readable"
            ) from error
        if len(token) > 4_096:
            raise DeveloperCollaborationClientError(
                "developer token file is too large"
            )
    else:
        token = environment_value.strip()
    if len(token) < 32:
        raise DeveloperCollaborationClientError(
            "LILIES_COLLABORATION_DEVELOPER_TOKEN or a private token file is required"
        )
    return token


class DeveloperCollaborationClient:
    """Machine-facing client for the approved developer inbox.

    The bearer is accepted only as constructor state and is sent only in the
    Authorization header. It is never included in URLs, request bodies, or
    public exceptions.
    """

    owner_id = "codex-developer"

    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.base_url = normalize_platform_base_url(base_url)
        self._access_token = access_token
        self._transport = transport
        self._timeout = timeout_seconds

    def inbox(
        self,
        *,
        after: int = 0,
        limit: int = 100,
        route: str | None = None,
    ) -> DeveloperInboxResponse:
        if after < 0:
            raise ValueError("after must be non-negative")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        query: dict[str, str | int] = {"after": after, "limit": limit}
        if route is not None:
            query["route"] = route
        return DeveloperInboxResponse.model_validate(
            self._request(
                "GET",
                f"/api/v1/developer/collaboration/inbox?{urlencode(query)}",
            )
        )

    def acquire_lease(
        self,
        report_id: UUID,
        *,
        expected_report_revision: int,
        idempotency_key: str,
        ttl_seconds: int = 900,
    ) -> DeveloperLease:
        return DeveloperLease.model_validate(
            self._request(
                "POST",
                f"/api/v1/developer/collaboration/reports/{report_id}/lease",
                {
                    "idempotency_key": idempotency_key,
                    "expected_report_revision": expected_report_revision,
                    "owner_id": self.owner_id,
                    "ttl_seconds": ttl_seconds,
                },
            )
        )

    def renew_lease(
        self,
        report_id: UUID,
        *,
        expected_lease_revision: int,
        idempotency_key: str,
        ttl_seconds: int = 900,
    ) -> DeveloperLease:
        return DeveloperLease.model_validate(
            self._request(
                "POST",
                f"/api/v1/developer/collaboration/reports/{report_id}/lease/renew",
                {
                    "idempotency_key": idempotency_key,
                    "expected_lease_revision": expected_lease_revision,
                    "owner_id": self.owner_id,
                    "ttl_seconds": ttl_seconds,
                },
            )
        )

    def release_lease(
        self,
        report_id: UUID,
        *,
        expected_lease_revision: int,
        idempotency_key: str,
        reason: str,
    ) -> DeveloperLease:
        return DeveloperLease.model_validate(
            self._request(
                "POST",
                f"/api/v1/developer/collaboration/reports/{report_id}/lease/release",
                {
                    "idempotency_key": idempotency_key,
                    "expected_lease_revision": expected_lease_revision,
                    "owner_id": self.owner_id,
                    "reason": reason,
                },
            )
        )

    def respond(
        self,
        report_id: UUID,
        *,
        lease_id: UUID,
        expected_report_revision: int,
        idempotency_key: str,
        response: DeveloperResponsePayload,
    ) -> DeveloperResponse:
        return DeveloperResponse.model_validate(
            self._request(
                "POST",
                f"/api/v1/developer/collaboration/reports/{report_id}/responses",
                {
                    "idempotency_key": idempotency_key,
                    "lease_id": str(lease_id),
                    "lease_owner_id": self.owner_id,
                    "expected_report_revision": expected_report_revision,
                    "response": response.model_dump(mode="json", exclude_none=True),
                },
            )
        )

    def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    json=json_body,
                )
        except httpx.HTTPError as error:
            raise DeveloperCollaborationClientError(
                f"developer collaboration request failed: {type(error).__name__}"
            ) from error
        if response.status_code < 200 or response.status_code >= 300:
            message = "developer collaboration request was rejected"
            try:
                payload = response.json()
            except (json.JSONDecodeError, ValueError):
                payload = None
            if isinstance(payload, dict):
                detail = payload.get("detail")
                if isinstance(detail, dict):
                    candidate = detail.get("message") or detail.get("code")
                    if isinstance(candidate, str) and candidate:
                        message = candidate
            message = _SENSITIVE_TEXT.sub("[redacted]", message)
            message = message.replace(self._access_token, "[redacted]")
            raise DeveloperCollaborationClientError(
                message[:1_000],
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as error:
            raise DeveloperCollaborationClientError(
                "developer collaboration endpoint returned invalid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise DeveloperCollaborationClientError(
                "developer collaboration endpoint returned a non-object response"
            )
        return payload
