from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from agent_platform import local_lilies_client
from agent_platform.local_lilies_client import (
    LocalLiliesHttpClient,
    LocalLiliesProtocolError,
    LocalLiliesRemoteError,
)


class ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


class FirstChunkThenBlockStream(httpx.AsyncByteStream):
    def __init__(self, first_chunk: bytes) -> None:
        self.first_chunk = first_chunk
        self.closed = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.first_chunk
        await self.closed.wait()

    async def aclose(self) -> None:
        self.closed.set()


Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def _deep_json_object(depth: int) -> bytes:
    assert 0 < depth <= 2_000
    return (b'{"child":' * depth) + b"{}" + (b"}" * depth)


def _transport(
    body: bytes,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    chunk_bytes: int = 7_919,
    requests: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    chunks = [body[offset : offset + chunk_bytes] for offset in range(0, len(body), chunk_bytes)]

    async def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        response_headers = {"content-type": "application/json"}
        response_headers.update(headers or {})
        return httpx.Response(
            status_code,
            headers=response_headers,
            stream=ChunkedStream(chunks),
        )

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_json_request_freezes_a_literal_ipv6_loopback_origin() -> None:
    requests: list[httpx.Request] = []
    client = LocalLiliesHttpClient(
        transport=_transport(
            b'{"status":"ok"}',
            headers={"content-type": "application/json"},
            requests=requests,
        )
    )

    result = await client.health("http://[0:0:0:0:0:0:0:1]:8765/")

    assert result == {"status": "ok"}
    assert len(requests) == 1
    assert str(requests[0].url) == "http://[::1]:8765/local/v1/health"
    assert requests[0].headers["accept-encoding"] == "identity"


@pytest.mark.asyncio
async def test_usage_uses_authenticated_bounded_public_query() -> None:
    requests: list[httpx.Request] = []
    payload = {
        "schema_version": "1.0",
        "group_by": ["session", "stage", "model"],
        "items": [],
        "page": 1,
        "page_size": 100,
        "returned_count": 0,
        "total_items": 0,
        "total_pages": 0,
        "truncated": False,
    }
    client = LocalLiliesHttpClient(
        transport=_transport(json.dumps(payload).encode(), requests=requests)
    )

    result = await client.usage("http://127.0.0.1:8765", "paired-secret")

    assert result == payload
    assert len(requests) == 1
    assert requests[0].url.path == "/local/v1/usage"
    assert requests[0].url.params.multi_items() == [
        ("group_by", "session"),
        ("group_by", "stage"),
        ("group_by", "model"),
        ("page", "1"),
        ("page_size", "100"),
    ]
    assert requests[0].headers["authorization"] == "Bearer paired-secret"
    assert requests[0].headers["accept-encoding"] == "identity"

    with pytest.raises(LocalLiliesProtocolError, match="outside safe bounds"):
        await client.usage(
            "http://127.0.0.1:8765",
            "paired-secret",
            page_size=101,
        )
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8765",
        "https://127.0.0.1:8765",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://192.0.2.1:8765",
        "http://user@127.0.0.1:8765",
        "http://127.0.0.1:8765/not-an-origin",
        "http://127.0.0.1:8765?next=http://127.0.0.1",
        "http://[::1%25lo0]:8765",
        "http://[::1",
    ],
)
async def test_json_request_rejects_dns_remote_or_ambiguous_origins_without_io(
    base_url: str,
) -> None:
    requests: list[httpx.Request] = []
    client = LocalLiliesHttpClient(transport=_transport(b'{"status":"ok"}', requests=requests))

    with pytest.raises(LocalLiliesProtocolError, match="literal loopback"):
        await client.health(base_url)

    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_id",
    [
        ".",
        "..",
        "../health",
        "safe/../health",
        "%2e%2e",
        "%2E%2E",
        "%2fhealth",
        "%5Chealth",
        r"..\health",
        "unicode-会话",
    ],
)
async def test_dynamic_path_rejects_dot_encoded_or_backslash_escape_without_io(
    session_id: str,
) -> None:
    requests: list[httpx.Request] = []
    client = LocalLiliesHttpClient(transport=_transport(b'{"status":"ok"}', requests=requests))

    with pytest.raises(LocalLiliesProtocolError, match="request"):
        await client.get_session(
            "http://127.0.0.1:8765",
            "paired-secret",
            session_id,
        )

    assert requests == []


@pytest.mark.asyncio
async def test_redirect_is_rejected_without_following_location() -> None:
    requests: list[httpx.Request] = []
    client = LocalLiliesHttpClient(
        transport=_transport(
            b"redirect",
            status_code=302,
            headers={"location": "http://192.0.2.1:9999/local/v1/health"},
            requests=requests,
        )
    )

    with pytest.raises(LocalLiliesProtocolError, match="redirect rejected"):
        await client.health("http://127.0.0.1:8765")

    assert len(requests) == 1
    assert requests[0].url.host == "127.0.0.1"


@pytest.mark.asyncio
async def test_encoded_response_is_rejected_before_decompression() -> None:
    client = LocalLiliesHttpClient(
        transport=_transport(
            b"not-inflated",
            headers={"content-encoding": "gzip"},
        )
    )

    with pytest.raises(LocalLiliesProtocolError, match="encoding is not supported"):
        await client.health("http://127.0.0.1:8765")


@pytest.mark.asyncio
async def test_success_response_requires_exact_target_and_expected_media_type() -> None:
    wrong_media_client = LocalLiliesHttpClient(
        transport=_transport(
            b'{"status":"ok"}',
            headers={"content-type": "text/html"},
        )
    )
    with pytest.raises(LocalLiliesProtocolError, match="Content-Type"):
        await wrong_media_client.health("http://127.0.0.1:8765")

    wrong_target_response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        request=httpx.Request(
            "GET",
            "http://127.0.0.1:9999/local/v1/health",
        ),
    )
    with pytest.raises(LocalLiliesProtocolError, match="target"):
        LocalLiliesHttpClient._validate_response_security(
            wrong_target_response,
            expected_url="http://127.0.0.1:8765/local/v1/health",
        )


@pytest.mark.asyncio
async def test_invalid_or_oversized_declared_length_fails_before_body_read() -> None:
    invalid_client = LocalLiliesHttpClient(
        transport=_transport(
            b'{"status":"ok"}',
            headers={"content-length": "invalid"},
        )
    )
    with pytest.raises(LocalLiliesProtocolError, match="Content-Length"):
        await invalid_client.health("http://127.0.0.1:8765")

    oversized_client = LocalLiliesHttpClient(
        transport=_transport(
            b"",
            headers={"content-length": str(local_lilies_client._MAX_JSON_RESPONSE_BYTES + 1)},
        )
    )
    with pytest.raises(LocalLiliesProtocolError, match="exceeded"):
        await oversized_client.health("http://127.0.0.1:8765")


@pytest.mark.asyncio
async def test_json_response_accepts_exact_limit_and_rejects_one_more_byte() -> None:
    limit = local_lilies_client._MAX_JSON_RESPONSE_BYTES
    prefix = b'{"value":"'
    suffix = b'"}'
    exact = prefix + (b"x" * (limit - len(prefix) - len(suffix))) + suffix
    exact_client = LocalLiliesHttpClient(transport=_transport(exact))

    payload = await exact_client.health("http://127.0.0.1:8765")

    assert len(payload["value"]) == limit - len(prefix) - len(suffix)

    oversized_client = LocalLiliesHttpClient(transport=_transport(b" " * (limit + 1)))
    with pytest.raises(LocalLiliesProtocolError, match="exceeded"):
        await oversized_client.health("http://127.0.0.1:8765")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "depth",
    [local_lilies_client._MAX_JSON_NESTING_DEPTH + 1, 2_000],
)
async def test_success_json_rejects_bounded_excessive_nesting_without_recursion_leak(
    depth: int,
) -> None:
    body = _deep_json_object(depth)
    assert len(body) < local_lilies_client._MAX_JSON_RESPONSE_BYTES
    client = LocalLiliesHttpClient(transport=_transport(body))

    with pytest.raises(LocalLiliesProtocolError, match="invalid JSON|excessively nested"):
        await client.health("http://127.0.0.1:8765")


@pytest.mark.asyncio
async def test_error_response_is_bounded_before_parsing_or_rendering() -> None:
    limit = local_lilies_client._MAX_ERROR_RESPONSE_BYTES
    secret = b"must-not-be-rendered"
    client = LocalLiliesHttpClient(
        transport=_transport(
            (b"x" * (limit + 1)) + secret,
            status_code=422,
        )
    )

    with pytest.raises(LocalLiliesProtocolError, match="exceeded") as captured:
        await client.health("http://127.0.0.1:8765")

    assert secret.decode() not in str(captured.value)


@pytest.mark.asyncio
async def test_bounded_error_json_preserves_remote_status_contract() -> None:
    body = json.dumps({"detail": {"code": "pairing_expired"}}).encode()
    client = LocalLiliesHttpClient(transport=_transport(body, status_code=409))

    with pytest.raises(LocalLiliesRemoteError) as captured:
        await client.health("http://127.0.0.1:8765")

    assert captured.value.status_code == 409
    assert str(captured.value) == "pairing_expired"
    assert captured.value.payload == {"detail": {"code": "pairing_expired"}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "depth",
    [local_lilies_client._MAX_JSON_NESTING_DEPTH + 1, 2_000],
)
async def test_error_json_uses_safe_remote_fallback_for_excessive_nesting(
    depth: int,
) -> None:
    body = _deep_json_object(depth)
    assert len(body) < local_lilies_client._MAX_ERROR_RESPONSE_BYTES
    client = LocalLiliesHttpClient(transport=_transport(body, status_code=500))

    with pytest.raises(LocalLiliesRemoteError) as captured:
        await client.health("http://127.0.0.1:8765")

    assert captured.value.status_code == 500
    assert str(captured.value) == "local Lilies request failed"
    assert captured.value.payload is None


@pytest.mark.asyncio
async def test_json_streaming_remains_compatible_with_asgi_transport() -> None:
    app = FastAPI()

    @app.get("/local/v1/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "transport": "asgi"}

    client = LocalLiliesHttpClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 43130))
    )

    result = await client.health("http://127.0.0.1:8765")

    assert result == {"status": "ok", "transport": "asgi"}


@pytest.mark.asyncio
async def test_sse_parser_handles_chunk_splits_without_unbounded_line_buffering() -> None:
    encoded = ('id: 7\r\nevent: session.updated\r\ndata: {"message":"你好"}\r\n\r\n').encode()
    client = LocalLiliesHttpClient(
        transport=_transport(
            encoded,
            headers={"content-type": "text/event-stream"},
            chunk_bytes=2,
        )
    )

    events = await client.fetch_events(
        "http://127.0.0.1:8765",
        "access-token",
        "session-1",
        after=0,
        wait_seconds=1,
    )

    assert events == [
        {
            "seq": 7,
            "event": "session.updated",
            "data": {"message": "你好"},
        }
    ]


@pytest.mark.asyncio
async def test_sse_requires_media_type_bounded_id_and_object_payload() -> None:
    wrong_media = LocalLiliesHttpClient(transport=_transport(b'id: 1\ndata: {"ok":true}\n\n'))
    with pytest.raises(LocalLiliesProtocolError, match="Content-Type"):
        await wrong_media.fetch_events(
            "http://127.0.0.1:8765",
            "access-token",
            "session-1",
            after=0,
        )

    for body in (
        b'id: -1\ndata: {"ok":true}\n\n',
        b'id: 9223372036854775808\ndata: {"ok":true}\n\n',
        b"id: 1\ndata: []\n\n",
    ):
        client = LocalLiliesHttpClient(
            transport=_transport(
                body,
                headers={"content-type": "text/event-stream"},
            )
        )
        with pytest.raises(LocalLiliesProtocolError):
            await client.fetch_events(
                "http://127.0.0.1:8765",
                "access-token",
                "session-1",
                after=0,
                wait_seconds=1,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "depth",
    [local_lilies_client._MAX_JSON_NESTING_DEPTH + 1, 2_000],
)
async def test_sse_json_rejects_bounded_excessive_nesting_without_recursion_leak(
    depth: int,
) -> None:
    nested = _deep_json_object(depth)
    body = b"id: 1\ndata: " + nested + b"\n\n"
    assert len(nested) < local_lilies_client._MAX_SSE_EVENT_DATA_BYTES
    client = LocalLiliesHttpClient(
        transport=_transport(
            body,
            headers={"content-type": "text/event-stream"},
        )
    )

    with pytest.raises(LocalLiliesProtocolError, match="invalid SSE JSON|excessively nested"):
        await client.fetch_events(
            "http://127.0.0.1:8765",
            "access-token",
            "session-1",
            after=0,
            wait_seconds=1,
        )


@pytest.mark.asyncio
async def test_sse_delivers_small_first_event_without_waiting_for_stream_eof() -> None:
    stream = FirstChunkThenBlockStream(b'id: 1\ndata: {"ready":true}\n\n')

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    client = LocalLiliesHttpClient(transport=httpx.MockTransport(handler))

    events = await client.fetch_events(
        "http://127.0.0.1:8765",
        "access-token",
        "session-1",
        after=0,
        max_events=1,
        wait_seconds=0.1,
    )

    assert events == [
        {
            "seq": 1,
            "event": "message",
            "data": {"ready": True},
        }
    ]
    assert stream.closed.is_set()


@pytest.mark.asyncio
async def test_sse_line_without_newline_has_a_hard_byte_limit() -> None:
    body = b":" + (b"x" * (local_lilies_client._MAX_SSE_LINE_BYTES + 2))
    client = LocalLiliesHttpClient(
        transport=_transport(
            body,
            headers={"content-type": "text/event-stream"},
        )
    )

    with pytest.raises(LocalLiliesProtocolError, match="line exceeded"):
        await client.fetch_events(
            "http://127.0.0.1:8765",
            "access-token",
            "session-1",
            after=0,
            wait_seconds=1,
        )


@pytest.mark.asyncio
async def test_sse_event_data_has_a_hard_aggregate_byte_limit() -> None:
    line_data = b'"' + (b"x" * 65_000) + b'"'
    line = b"data: " + line_data + b"\n"
    count = (local_lilies_client._MAX_SSE_EVENT_DATA_BYTES // len(line_data)) + 2
    body = b"id: 1\n" + (line * count) + b"\n"
    client = LocalLiliesHttpClient(
        transport=_transport(
            body,
            headers={"content-type": "text/event-stream"},
        )
    )

    with pytest.raises(LocalLiliesProtocolError, match="event data exceeded"):
        await client.fetch_events(
            "http://127.0.0.1:8765",
            "access-token",
            "session-1",
            after=0,
            wait_seconds=1,
        )


@pytest.mark.asyncio
async def test_sse_event_has_a_hard_line_count_limit() -> None:
    body = b": keepalive\n" * (local_lilies_client._MAX_SSE_EVENT_LINES + 1)
    client = LocalLiliesHttpClient(
        transport=_transport(
            body,
            headers={"content-type": "text/event-stream"},
        )
    )

    with pytest.raises(LocalLiliesProtocolError, match="event exceeded its line limit"):
        await client.fetch_events(
            "http://127.0.0.1:8765",
            "access-token",
            "session-1",
            after=0,
            wait_seconds=1,
        )


@pytest.mark.asyncio
async def test_sse_batch_has_a_hard_total_byte_limit() -> None:
    event_data = json.dumps({"value": "x" * 5_000}).encode()
    event = b"id: 1\ndata: " + event_data + b"\n\n"
    count = (local_lilies_client._MAX_SSE_BATCH_BYTES // len(event)) + 2
    body = event * count
    client = LocalLiliesHttpClient(
        transport=_transport(
            body,
            headers={"content-type": "text/event-stream"},
        )
    )

    with pytest.raises(LocalLiliesProtocolError, match="batch exceeded"):
        await client.fetch_events(
            "http://127.0.0.1:8765",
            "access-token",
            "session-1",
            after=0,
            max_events=1_000,
            wait_seconds=1,
        )
