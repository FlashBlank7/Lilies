from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import ValidationError

from .lilies_models import AssignmentSubmissionResult, BuildAssignment


class LiliesDaemonClientError(RuntimeError):
    """A bounded local-daemon request failed without exposing credentials."""


class LiliesDaemonClient:
    """Async platform-side adapter for the strict local assignment endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        host = parsed.hostname or ""
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.casefold() == "localhost"
        if (
            parsed.scheme != "http"
            or not loopback
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Lilies daemon base_url must be an uncredentialed loopback HTTP URL")
        if not access_token:
            raise ValueError("Lilies daemon access token is required")
        if timeout <= 0:
            raise ValueError("Lilies daemon timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self._access_token = access_token
        self.timeout = timeout
        self.transport = transport

    def __repr__(self) -> str:
        return f"LiliesDaemonClient(base_url={self.base_url!r})"

    async def submit_assignment(
        self,
        session_id: UUID | str,
        assignment: BuildAssignment | dict[str, object],
    ) -> AssignmentSubmissionResult:
        try:
            parsed_session_id = UUID(str(session_id))
            validated = (
                assignment
                if isinstance(assignment, BuildAssignment)
                else BuildAssignment.model_validate(assignment)
            )
        except (ValueError, ValidationError) as error:
            raise LiliesDaemonClientError("invalid local assignment request") from error

        headers = {
            "accept": "application/json",
            "authorization": f"Bearer {self._access_token}",
        }
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"/local/v1/sessions/{parsed_session_id}/assignments",
                    json=validated.model_dump(mode="json", exclude_none=True),
                    headers=headers,
                )
        except httpx.HTTPError as error:
            raise LiliesDaemonClientError("local Lilies daemon is unavailable") from error
        if response.status_code >= 400:
            raise LiliesDaemonClientError(
                f"local Lilies daemon rejected assignment (status {response.status_code})"
            )
        try:
            return AssignmentSubmissionResult.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise LiliesDaemonClientError(
                "local Lilies daemon returned an invalid assignment receipt"
            ) from error
