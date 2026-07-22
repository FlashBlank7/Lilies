from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx


class LocalLiliesClientError(RuntimeError):
    """Base error for the platform-to-daemon public HTTP boundary."""


class LocalLiliesUnavailable(LocalLiliesClientError):
    pass


class LocalLiliesProtocolError(LocalLiliesClientError):
    pass


class LocalLiliesRemoteError(LocalLiliesClientError):
    def __init__(self, status_code: int, message: str, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class LocalLiliesHttpClient:
    """Narrow client for the public ``/local/v1`` daemon contract.

    This adapter deliberately knows only HTTP payloads.  It does not import the
    daemon storage or service implementation, which keeps the process boundary
    real in production and replaceable by an ASGI/fake transport in tests.
    """

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("local Lilies timeout must be positive")
        self.transport = transport
        self.timeout = httpx.Timeout(timeout_seconds)

    async def health(self, base_url: str) -> dict[str, Any]:
        return await self._json("GET", base_url, "/local/v1/health")

    async def exchange_pairing(
        self,
        base_url: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            base_url,
            "/local/v1/pairings/exchange",
            json_payload=payload,
        )

    async def status(self, base_url: str, access_token: str) -> dict[str, Any]:
        return await self._json(
            "GET",
            base_url,
            "/local/v1/status",
            access_token=access_token,
        )

    async def create_session(
        self,
        base_url: str,
        access_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            base_url,
            "/local/v1/sessions",
            access_token=access_token,
            json_payload=payload,
        )

    async def get_session(
        self,
        base_url: str,
        access_token: str,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._json(
            "GET",
            base_url,
            f"/local/v1/sessions/{session_id}",
            access_token=access_token,
        )

    async def provision_credential(
        self,
        base_url: str,
        access_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            base_url,
            "/local/v1/credentials/provision",
            access_token=access_token,
            json_payload=payload,
        )

    async def revoke_credential(
        self,
        base_url: str,
        access_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            base_url,
            "/local/v1/credentials/revoke",
            access_token=access_token,
            json_payload=payload,
        )

    async def submit_assignment(
        self,
        base_url: str,
        access_token: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            base_url,
            f"/local/v1/sessions/{session_id}/assignments",
            access_token=access_token,
            json_payload=payload,
        )

    async def cancel_session(
        self,
        base_url: str,
        access_token: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            base_url,
            f"/local/v1/sessions/{session_id}/cancel",
            access_token=access_token,
            json_payload=payload,
        )

    async def resume_session(
        self,
        base_url: str,
        access_token: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            base_url,
            f"/local/v1/sessions/{session_id}/resume",
            access_token=access_token,
            json_payload=payload,
        )

    async def acknowledge_events(
        self,
        base_url: str,
        access_token: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            base_url,
            f"/local/v1/sessions/{session_id}/acks",
            access_token=access_token,
            json_payload=payload,
        )

    async def fetch_events(
        self,
        base_url: str,
        access_token: str,
        session_id: str,
        *,
        after: int,
        max_events: int = 100,
        wait_seconds: float = 0.25,
    ) -> list[dict[str, Any]]:
        """Read one bounded SSE batch and then close the stream.

        The daemon stream is intentionally long lived.  Platform relay workers
        consume at most ``max_events`` or one idle timeout per poll so a stale
        daemon cannot pin a request or platform shutdown.
        """

        url = self._url(base_url, f"/local/v1/sessions/{session_id}/events")
        headers = self._headers(access_token)
        headers["Accept"] = "text/event-stream"
        headers["Last-Event-ID"] = str(max(0, after))
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "GET",
                    url,
                    headers=headers,
                    params={"after": max(0, after)},
                ) as response:
                    await self._raise_for_status(response)
                    events: list[dict[str, Any]] = []
                    iterator = response.aiter_lines()
                    while len(events) < max(1, min(max_events, 1000)):
                        try:
                            event = await asyncio.wait_for(
                                self._next_sse_event(iterator),
                                timeout=max(0.01, wait_seconds),
                            )
                        except (TimeoutError, StopAsyncIteration):
                            break
                        if event is not None:
                            events.append(event)
                    return events
        except LocalLiliesClientError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise LocalLiliesUnavailable(f"local Lilies daemon unavailable: {error}") from error
        except httpx.HTTPError as error:
            raise LocalLiliesUnavailable(f"local Lilies daemon HTTP failure: {error}") from error

    async def _json(
        self,
        method: str,
        base_url: str,
        path: str,
        *,
        access_token: str | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.request(
                    method,
                    self._url(base_url, path),
                    headers=self._headers(access_token),
                    json=json_payload,
                )
            await self._raise_for_status(response)
        except LocalLiliesClientError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise LocalLiliesUnavailable(f"local Lilies daemon unavailable: {error}") from error
        except httpx.HTTPError as error:
            raise LocalLiliesUnavailable(f"local Lilies daemon HTTP failure: {error}") from error
        try:
            payload = response.json()
        except ValueError as error:
            raise LocalLiliesProtocolError(
                f"local Lilies returned invalid JSON for {method} {path}"
            ) from error
        if not isinstance(payload, dict):
            raise LocalLiliesProtocolError(
                f"local Lilies returned a non-object response for {method} {path}"
            )
        return payload

    @staticmethod
    async def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text[:2_000]
        message = "local Lilies request failed"
        if isinstance(payload, dict):
            detail = payload.get("detail") or payload.get("error") or payload.get("message")
            if isinstance(detail, dict):
                message = str(detail.get("message") or detail.get("code") or message)
            elif detail:
                message = str(detail)
        elif payload:
            message = str(payload)
        raise LocalLiliesRemoteError(response.status_code, message, payload)

    @staticmethod
    async def _next_sse_event(lines: AsyncIterator[str]) -> dict[str, Any] | None:
        event_id: int | None = None
        event_type = "message"
        data_lines: list[str] = []
        async for line in lines:
            if not line:
                if event_id is None and not data_lines:
                    continue
                raw_data = "\n".join(data_lines)
                try:
                    data: Any = json.loads(raw_data) if raw_data else {}
                except json.JSONDecodeError as error:
                    raise LocalLiliesProtocolError("local Lilies emitted invalid SSE JSON") from error
                if event_id is None:
                    raise LocalLiliesProtocolError("local Lilies SSE event omitted its durable id")
                return {"seq": event_id, "event": event_type, "data": data}
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value.lstrip(" ")
            if field == "id":
                try:
                    event_id = int(value)
                except ValueError as error:
                    raise LocalLiliesProtocolError("local Lilies emitted a non-integer SSE id") from error
            elif field == "event":
                event_type = value or "message"
            elif field == "data":
                data_lines.append(value)
        raise StopAsyncIteration

    @staticmethod
    def _url(base_url: str, path: str) -> str:
        parsed = urlsplit(base_url)
        host = parsed.hostname or ""
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host.casefold() == "localhost"
        if (
            parsed.scheme != "http"
            or not is_loopback
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise LocalLiliesProtocolError(
                "local Lilies base URL must be an uncredentialed loopback HTTP origin"
            )
        if not path.startswith("/local/v1/"):
            raise LocalLiliesProtocolError("local Lilies request left the public local API")
        return f"{base_url.rstrip('/')}{path}"

    @staticmethod
    def _headers(access_token: str | None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers
