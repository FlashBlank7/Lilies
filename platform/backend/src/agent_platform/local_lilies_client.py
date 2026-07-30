from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx


_HTTP_READ_CHUNK_BYTES = 16_384
_MAX_JSON_RESPONSE_BYTES = 1_048_576
_MAX_ERROR_RESPONSE_BYTES = 65_536
_MAX_SSE_LINE_BYTES = 65_536
_MAX_SSE_EVENT_DATA_BYTES = 1_048_576
_MAX_SSE_EVENT_LINES = 4_096
_MAX_SSE_BATCH_BYTES = 4_194_304
_MAX_EVENT_ID = 9_223_372_036_854_775_807
_MAX_JSON_NESTING_DEPTH = 64
_ERROR_MEDIA_TYPES = frozenset({"application/json", "text/plain"})


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

    async def observability_snapshot(
        self,
        base_url: str,
        access_token: str,
    ) -> dict[str, Any]:
        return await self._json(
            "GET",
            base_url,
            "/local/v1/observability/snapshot",
            access_token=access_token,
        )

    async def usage(
        self,
        base_url: str,
        access_token: str,
        *,
        group_by: tuple[str, ...] = ("session", "stage", "model"),
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        if (
            not group_by
            or len(group_by) > 3
            or len(set(group_by)) != len(group_by)
            or any(dimension not in {"session", "stage", "model"} for dimension in group_by)
            or isinstance(page, bool)
            or not 1 <= page <= 1_000
            or isinstance(page_size, bool)
            or not 1 <= page_size <= 100
        ):
            raise LocalLiliesProtocolError("local Lilies usage query is outside safe bounds")
        params: list[tuple[str, str | int]] = [("group_by", dimension) for dimension in group_by]
        params.extend((("page", page), ("page_size", page_size)))
        return await self._json(
            "GET",
            base_url,
            "/local/v1/usage",
            access_token=access_token,
            params=params,
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

    async def list_session_messages(
        self,
        base_url: str,
        access_token: str,
        session_id: str,
        *,
        limit: int = 20,
        before: str | None = None,
    ) -> dict[str, Any]:
        if (
            isinstance(limit, bool)
            or not 1 <= limit <= 20
            or (before is not None and re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                before,
                flags=re.IGNORECASE,
            ) is None)
        ):
            raise LocalLiliesProtocolError(
                "local Lilies message-history query is outside safe bounds"
            )
        params: list[tuple[str, str | int]] = [("limit", limit)]
        if before is not None:
            params.append(("before", before))
        return await self._json(
            "GET",
            base_url,
            f"/local/v1/sessions/{session_id}/messages",
            access_token=access_token,
            params=params,
        )

    async def send_session_message(
        self,
        base_url: str,
        access_token: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            base_url,
            f"/local/v1/sessions/{session_id}/messages",
            access_token=access_token,
            json_payload=payload,
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

    async def stage_formal_workspace(
        self,
        base_url: str,
        access_token: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            base_url,
            f"/local/v1/sessions/{session_id}/formal-workspace",
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

    async def resolve_permission(
        self,
        base_url: str,
        access_token: str,
        session_id: str,
        request_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            base_url,
            f"/local/v1/sessions/{session_id}/permissions/{request_id}",
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
        params = {"after": max(0, after)}
        expected_url = str(httpx.URL(url, params=params))
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
                    params=params,
                ) as response:
                    self._validate_response_security(
                        response,
                        expected_url=expected_url,
                    )
                    await self._raise_for_status(response)
                    self._require_media_type(response, "text/event-stream")
                    events: list[dict[str, Any]] = []
                    iterator = self._bounded_sse_lines(response)
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
        params: Sequence[tuple[str, str | int]] | None = None,
    ) -> dict[str, Any]:
        url = self._url(base_url, path)
        expected_url = str(httpx.URL(url, params=params)) if params else url
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    method,
                    url,
                    headers=self._headers(access_token),
                    json=json_payload,
                    params=params,
                ) as response:
                    self._validate_response_security(
                        response,
                        expected_url=expected_url,
                    )
                    await self._raise_for_status(response)
                    self._require_media_type(response, "application/json")
                    raw_payload = await self._read_bounded_bytes(
                        response,
                        limit=_MAX_JSON_RESPONSE_BYTES,
                        description="JSON response",
                    )
        except LocalLiliesClientError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise LocalLiliesUnavailable(f"local Lilies daemon unavailable: {error}") from error
        except httpx.HTTPError as error:
            raise LocalLiliesUnavailable(f"local Lilies daemon HTTP failure: {error}") from error
        try:
            payload = json.loads(raw_payload)
        except (UnicodeDecodeError, ValueError, RecursionError) as error:
            raise LocalLiliesProtocolError(
                f"local Lilies returned invalid JSON for {method} {path}"
            ) from error
        if not self._json_nesting_is_safe(payload):
            raise LocalLiliesProtocolError(
                f"local Lilies returned excessively nested JSON for {method} {path}"
            )
        if not isinstance(payload, dict):
            raise LocalLiliesProtocolError(
                f"local Lilies returned a non-object response for {method} {path}"
            )
        return payload

    @classmethod
    async def _raise_for_status(cls, response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        if 300 <= response.status_code < 400:
            await LocalLiliesHttpClient._read_bounded_bytes(
                response,
                limit=_MAX_ERROR_RESPONSE_BYTES,
                description="redirect response",
            )
            raise LocalLiliesProtocolError(
                f"local Lilies redirect rejected with status {response.status_code}"
            )
        raw_payload = await LocalLiliesHttpClient._read_bounded_bytes(
            response,
            limit=_MAX_ERROR_RESPONSE_BYTES,
            description="error response",
        )
        media_type = cls._media_type(response)
        if media_type not in _ERROR_MEDIA_TYPES and not media_type.endswith("+json"):
            raise LocalLiliesRemoteError(
                response.status_code,
                "local Lilies request failed",
            )
        try:
            payload: Any = json.loads(raw_payload)
        except RecursionError:
            raise LocalLiliesRemoteError(
                response.status_code,
                "local Lilies request failed",
            ) from None
        except (UnicodeDecodeError, ValueError):
            payload = raw_payload.decode("utf-8", errors="replace")[:2_000]
        if not cls._json_nesting_is_safe(payload):
            raise LocalLiliesRemoteError(
                response.status_code,
                "local Lilies request failed",
            )
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
    async def _read_bounded_bytes(
        response: httpx.Response,
        *,
        limit: int,
        description: str,
    ) -> bytes:
        declared = LocalLiliesHttpClient._content_length(response)
        if declared is not None and declared > limit:
            raise LocalLiliesProtocolError(f"local Lilies {description} exceeded {limit} bytes")
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_raw(chunk_size=_HTTP_READ_CHUNK_BYTES):
            total += len(chunk)
            if total > limit:
                raise LocalLiliesProtocolError(f"local Lilies {description} exceeded {limit} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    async def _bounded_sse_lines(
        response: httpx.Response,
    ) -> AsyncIterator[str]:
        declared = LocalLiliesHttpClient._content_length(response)
        if declared is not None and declared > _MAX_SSE_BATCH_BYTES:
            raise LocalLiliesProtocolError("local Lilies SSE batch exceeded its byte limit")
        buffer = bytearray()
        batch_bytes = 0
        async for chunk in response.aiter_raw():
            batch_bytes += len(chunk)
            if batch_bytes > _MAX_SSE_BATCH_BYTES:
                raise LocalLiliesProtocolError("local Lilies SSE batch exceeded its byte limit")
            buffer.extend(chunk)
            while True:
                line_end = buffer.find(b"\n")
                if line_end < 0:
                    break
                raw_line = bytes(buffer[:line_end])
                del buffer[: line_end + 1]
                if raw_line.endswith(b"\r"):
                    raw_line = raw_line[:-1]
                if len(raw_line) > _MAX_SSE_LINE_BYTES:
                    raise LocalLiliesProtocolError("local Lilies SSE line exceeded its byte limit")
                try:
                    yield raw_line.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise LocalLiliesProtocolError(
                        "local Lilies emitted a non-UTF-8 SSE line"
                    ) from error
            if len(buffer) > _MAX_SSE_LINE_BYTES + 1:
                raise LocalLiliesProtocolError("local Lilies SSE line exceeded its byte limit")
        if buffer:
            raw_line = bytes(buffer)
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
            if len(raw_line) > _MAX_SSE_LINE_BYTES:
                raise LocalLiliesProtocolError("local Lilies SSE line exceeded its byte limit")
            try:
                yield raw_line.decode("utf-8")
            except UnicodeDecodeError as error:
                raise LocalLiliesProtocolError(
                    "local Lilies emitted a non-UTF-8 SSE line"
                ) from error

    @staticmethod
    async def _next_sse_event(lines: AsyncIterator[str]) -> dict[str, Any] | None:
        event_id: int | None = None
        event_type = "message"
        data_lines: list[str] = []
        data_bytes = 0
        event_lines = 0
        async for line in lines:
            event_lines += 1
            if event_lines > _MAX_SSE_EVENT_LINES:
                raise LocalLiliesProtocolError("local Lilies SSE event exceeded its line limit")
            if not line:
                if event_id is None and not data_lines:
                    continue
                raw_data = "\n".join(data_lines)
                try:
                    data: Any = json.loads(raw_data) if raw_data else {}
                except (json.JSONDecodeError, RecursionError) as error:
                    raise LocalLiliesProtocolError(
                        "local Lilies emitted invalid SSE JSON"
                    ) from error
                if not LocalLiliesHttpClient._json_nesting_is_safe(data):
                    raise LocalLiliesProtocolError(
                        "local Lilies emitted excessively nested SSE JSON"
                    )
                if event_id is None:
                    raise LocalLiliesProtocolError("local Lilies SSE event omitted its durable id")
                if not isinstance(data, dict):
                    raise LocalLiliesProtocolError("local Lilies emitted a non-object SSE payload")
                return {"seq": event_id, "event": event_type, "data": data}
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value.lstrip(" ")
            if field == "id":
                if not value.isdigit() or len(value) > 19 or int(value) > _MAX_EVENT_ID:
                    raise LocalLiliesProtocolError("local Lilies emitted a non-integer SSE id")
                event_id = int(value)
            elif field == "event":
                event_type = value or "message"
            elif field == "data":
                value_bytes = len(value.encode("utf-8"))
                data_bytes += value_bytes + (1 if data_lines else 0)
                if data_bytes > _MAX_SSE_EVENT_DATA_BYTES:
                    raise LocalLiliesProtocolError(
                        "local Lilies SSE event data exceeded its byte limit"
                    )
                data_lines.append(value)
        raise StopAsyncIteration

    @staticmethod
    def _url(base_url: str, path: str) -> str:
        try:
            parsed = urlsplit(base_url)
            host = parsed.hostname or ""
            literal_address = ipaddress.ip_address(host)
            port = parsed.port
        except ValueError as error:
            raise LocalLiliesProtocolError(
                "local Lilies base URL must be a literal loopback HTTP origin with an explicit port"
            ) from error
        if (
            parsed.scheme != "http"
            or not literal_address.is_loopback
            or "%" in host
            or port is None
            or not 1 <= port <= 65_535
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise LocalLiliesProtocolError(
                "local Lilies base URL must be a literal loopback HTTP origin with an explicit port"
            )
        parsed_path = urlsplit(path)
        try:
            expected_raw_path = path.encode("ascii")
        except UnicodeEncodeError as error:
            raise LocalLiliesProtocolError(
                "local Lilies request path must be canonical ASCII"
            ) from error
        segments = path.split("/")
        if (
            not path.startswith("/local/v1/")
            or parsed_path.scheme
            or parsed_path.netloc
            or parsed_path.query
            or parsed_path.fragment
            or parsed_path.path != path
            or "%" in path
            or "\\" in path
            or any(segment in {".", ".."} for segment in segments)
            or any(not segment for segment in segments[1:])
            or re.fullmatch(r"/local/v1/[A-Za-z0-9._~/-]+", path) is None
        ):
            raise LocalLiliesProtocolError("local Lilies request left the public local API")
        frozen_host = (
            f"[{literal_address.compressed}]"
            if literal_address.version == 6
            else literal_address.compressed
        )
        frozen_url = httpx.URL(f"http://{frozen_host}:{port}{path}")
        if frozen_url.raw_path != expected_raw_path:
            raise LocalLiliesProtocolError(
                "local Lilies request path changed during URL construction"
            )
        return str(frozen_url)

    @staticmethod
    def _headers(access_token: str | None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    @staticmethod
    def _content_length(response: httpx.Response) -> int | None:
        raw = response.headers.get("content-length")
        if raw is None:
            return None
        if (
            len(raw) > 20
            or not raw.isascii()
            or not raw.isdecimal()
        ):
            raise LocalLiliesProtocolError("local Lilies returned an invalid Content-Length")
        try:
            return int(raw)
        except ValueError as error:
            raise LocalLiliesProtocolError(
                "local Lilies returned an invalid Content-Length"
            ) from error

    @staticmethod
    def _json_nesting_is_safe(payload: Any) -> bool:
        stack: list[tuple[Any, int]] = [(payload, 0)]
        while stack:
            value, parent_depth = stack.pop()
            if isinstance(value, dict):
                depth = parent_depth + 1
                if depth > _MAX_JSON_NESTING_DEPTH:
                    return False
                stack.extend((item, depth) for item in value.values())
            elif isinstance(value, list):
                depth = parent_depth + 1
                if depth > _MAX_JSON_NESTING_DEPTH:
                    return False
                stack.extend((item, depth) for item in value)
        return True

    @staticmethod
    def _media_type(response: httpx.Response) -> str:
        return response.headers.get("content-type", "").partition(";")[0].strip().casefold()

    @classmethod
    def _require_media_type(
        cls,
        response: httpx.Response,
        expected: str,
    ) -> None:
        media_type = cls._media_type(response)
        valid = media_type == expected or (
            expected == "application/json" and media_type.endswith("+json")
        )
        if not valid:
            raise LocalLiliesProtocolError("local Lilies response Content-Type is not supported")

    @staticmethod
    def _validate_response_security(
        response: httpx.Response,
        *,
        expected_url: str,
    ) -> None:
        if response.history or response.url != httpx.URL(expected_url):
            raise LocalLiliesProtocolError(
                "local Lilies response target does not match the request"
            )
        content_encoding = response.headers.get("content-encoding", "identity")
        if content_encoding.casefold().strip() not in {"", "identity"}:
            raise LocalLiliesProtocolError(
                "local Lilies response content encoding is not supported"
            )
