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
        self._developer_response_revisions: dict[UUID, dict[str, Any]] = {}

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
        effective_payload, resolution = self._resolve_reprobe_revision(
            report_id,
            payload,
        )
        result = await self._request(
            "POST",
            f"reports/{report_id}/reprobes",
            json_payload=effective_payload,
        )
        if resolution is not None and result.ok:
            result.data["client_report_revision_resolution"] = resolution
        return result

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
        result = await self._request("GET", "events", query=query)
        if result.ok:
            transitions = self._capture_developer_response_revisions(result.data)
            if transitions:
                result.data["client_report_revision_transitions"] = transitions
        return result

    async def submit_verification_claim(
        self,
        payload: dict[str, Any],
    ) -> CollaborationHttpResult:
        return await self._request("POST", "verification-claims", json_payload=payload)

    async def prepare_formal_run_archive(
        self,
        payload: dict[str, Any],
    ) -> CollaborationHttpResult:
        return await self._request("POST", "formal-run-archives", json_payload=payload)

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
        channel_url = f"{self.base_url}/api/v1/collaboration/channels/{self.channel_id}"
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

    def _capture_developer_response_revisions(
        self,
        data: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Remember exact report revisions produced by visible developer responses.

        A persisted ``developer_response.v1`` consumes the revision carried in
        its payload and atomically advances that same report by exactly one
        revision.  The payload intentionally remains bound to the consumed
        revision, while the subsequent reprobe compare-and-set must use the
        resulting revision.  Derivation is deliberately limited to this exact
        event; arbitrary report revisions are never incremented.
        """

        raw_events = data.get("events")
        if not isinstance(raw_events, list):
            return []
        transitions: list[dict[str, Any]] = []
        for raw_event in raw_events:
            transition = self._developer_response_revision(raw_event)
            if transition is None:
                continue
            report_id = UUID(transition["report_id"])
            previous = self._developer_response_revisions.get(report_id)
            if previous is None or int(transition["source_event_seq"]) > int(
                previous["source_event_seq"]
            ):
                self._developer_response_revisions[report_id] = transition
            transitions.append(transition)
        return transitions

    def _developer_response_revision(
        self,
        raw_event: Any,
    ) -> dict[str, Any] | None:
        if (
            not isinstance(raw_event, dict)
            or raw_event.get("payload_schema") != "collaboration.developer_response.v1"
            or raw_event.get("message_type") != "developer_response"
            or raw_event.get("sender_role") != "codex"
            or raw_event.get("channel_id") != str(self.channel_id)
        ):
            return None
        payload = raw_event.get("payload")
        if not isinstance(payload, dict) or payload.get("channel_id") != str(
            self.channel_id
        ):
            return None
        raw_revision = payload.get("report_revision")
        raw_seq = raw_event.get("seq")
        if (
            isinstance(raw_revision, bool)
            or not isinstance(raw_revision, int)
            or raw_revision < 1
            or isinstance(raw_seq, bool)
            or not isinstance(raw_seq, int)
            or raw_seq < 1
        ):
            return None
        try:
            correlation_id = UUID(str(raw_event.get("correlation_id")))
            payload_report_id = UUID(str(payload.get("report_id")))
            source_message_id = UUID(str(raw_event.get("message_id")))
        except (TypeError, ValueError, AttributeError):
            return None
        if correlation_id != payload_report_id:
            return None
        resulting_revision = raw_revision + 1
        return {
            "schema_version": "1.0",
            "report_id": str(correlation_id),
            "source_message_id": str(source_message_id),
            "source_event_seq": raw_seq,
            "consumed_report_revision": raw_revision,
            "resulting_report_revision": resulting_revision,
            "reprobe_expected_report_revision": resulting_revision,
            "derivation": "developer_response_v1_atomic_increment",
        }

    def _resolve_reprobe_revision(
        self,
        report_id: UUID,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        effective_payload = dict(payload)
        transition = self._developer_response_revisions.get(report_id)
        requested_revision = payload.get("expected_report_revision")
        if (
            transition is None
            or isinstance(requested_revision, bool)
            or not isinstance(requested_revision, int)
            or requested_revision != int(transition["consumed_report_revision"])
        ):
            return effective_payload, None
        resulting_revision = int(transition["resulting_report_revision"])
        effective_payload["expected_report_revision"] = resulting_revision
        return effective_payload, {
            **transition,
            "requested_report_revision": requested_revision,
            "effective_report_revision": resulting_revision,
        }
