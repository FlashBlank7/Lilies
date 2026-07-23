from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field


class CollaborationHttpError(RuntimeError):
    """A public collaboration endpoint returned an unsuccessful response."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable


class CollaborationHttpResult(BaseModel):
    """Bounded, model-safe result returned by a collaboration HTTP tool."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    status_code: int = Field(ge=100, le=599)
    data: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None


class LiliesCollaborationClient:
    """HTTP-only client for one temporary, assignment-bound channel.

    The bearer is deliberately distinct from the platform workflow bearer.  It
    is retained only by the daemon's private credential store and is never
    included in a BuildAssignment, tool input, result, or exception message.
    """

    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        channel_id: UUID,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._access_token = access_token
        self.channel_id = channel_id
        self.timeout_seconds = timeout_seconds
        self._transport = transport

    async def submit_report(self, payload: dict[str, Any]) -> CollaborationHttpResult:
        return await self._request("POST", "reports", json_payload=payload)

    async def revise_report(
        self,
        report_id: UUID,
        payload: dict[str, Any],
    ) -> CollaborationHttpResult:
        return await self._request(
            "POST",
            f"reports/{report_id}/revisions",
            json_payload=payload,
        )

    async def submit_reprobe(
        self,
        report_id: UUID,
        payload: dict[str, Any],
    ) -> CollaborationHttpResult:
        return await self._request(
            "POST",
            f"reports/{report_id}/reprobes",
            json_payload=payload,
        )

    async def withdraw_report(
        self,
        report_id: UUID,
        payload: dict[str, Any],
    ) -> CollaborationHttpResult:
        return await self._request(
            "POST",
            f"reports/{report_id}/withdrawals",
            json_payload=payload,
        )

    async def channel_state(self) -> CollaborationHttpResult:
        return await self._request("GET", "")

    async def read_updates(
        self,
        *,
        after: int | None = None,
        limit: int = 200,
        history_replay: bool = False,
    ) -> CollaborationHttpResult:
        query: dict[str, str | int] = {"limit": limit, "format": "json"}
        if after is not None:
            query["after"] = after
        if history_replay:
            query["history_replay"] = "true"
        return await self._request("GET", "events", query=query)

    async def submit_verification_claim(
        self,
        payload: dict[str, Any],
    ) -> CollaborationHttpResult:
        return await self._request("POST", "verification-claims", json_payload=payload)

    async def acknowledge(self, payload: dict[str, Any]) -> CollaborationHttpResult:
        return await self._request("POST", "acks", json_payload=payload)

    async def _request(
        self,
        method: str,
        suffix: str,
        *,
        json_payload: dict[str, Any] | None = None,
        query: dict[str, str | int] | None = None,
    ) -> CollaborationHttpResult:
        channel_url = (
            f"{self.base_url}/api/v1/collaboration/channels/{self.channel_id}"
        )
        url = f"{channel_url}/{suffix}" if suffix else channel_url
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    method,
                    url,
                    headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "Accept": "application/json",
                    },
                    json=json_payload,
                    params=query,
                )
        except httpx.HTTPError:
            return CollaborationHttpResult(
                ok=False,
                status_code=503,
                error={
                    "code": "collaboration_unavailable",
                    "message": "the temporary collaboration endpoint is unavailable",
                    "retryable": True,
                },
            )
        return self._decode(response)

    @staticmethod
    def _decode(response: httpx.Response) -> CollaborationHttpResult:
        try:
            body = response.json()
        except ValueError:
            body = None
        if 200 <= response.status_code < 300 and isinstance(body, dict):
            return CollaborationHttpResult(
                ok=True,
                status_code=response.status_code,
                data=body,
            )
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, dict):
            code = str(detail.get("code") or "collaboration_request_failed")[:160]
            message = str(
                detail.get("message") or "the collaboration request was rejected"
            )[:1_000]
            retryable = bool(detail.get("retryable", response.status_code >= 500))
        else:
            code = "collaboration_request_failed"
            message = "the collaboration request was rejected"
            retryable = response.status_code >= 500
        return CollaborationHttpResult(
            ok=False,
            status_code=response.status_code,
            error={"code": code, "message": message, "retryable": retryable},
        )
