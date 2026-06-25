from __future__ import annotations

import asyncio
import json
import os
from itertools import count
from typing import Any

import httpx

from ..models import MCPServerSpec


class MCPError(RuntimeError):
    pass


class MCPClient:
    """Small JSON-RPC MCP client supporting HTTP and line-oriented stdio servers."""

    def __init__(self) -> None:
        self._ids = count(1)

    async def call_tool(
        self, server: MCPServerSpec, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if server.transport == "http":
            return await self._call_http(server, tool_name, arguments)
        return await self._call_stdio(server, tool_name, arguments)

    async def _call_http(
        self, server: MCPServerSpec, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        request = self._request("tools/call", {"name": tool_name, "arguments": arguments})
        headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
        headers.update(server.headers)
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(server.url or "", headers=headers, json=request)
            if response.status_code >= 400:
                raise MCPError(f"MCP HTTP {response.status_code}: {response.text[:1000]}")
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                for line in response.text.splitlines():
                    if line.startswith("data:"):
                        payload = json.loads(line[5:].strip())
                        return self._unwrap(payload)
                raise MCPError("MCP server returned no SSE data")
            return self._unwrap(response.json())

    async def _call_stdio(
        self, server: MCPServerSpec, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            server.command or "",
            *server.args,
            env={**os.environ, **server.env} if server.env else None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdin and process.stdout
        try:
            await self._stdio_exchange(process, self._request("initialize", {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "agent-platform", "version": "0.1.0"},
            }))
            process.stdin.write(
                (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode()
            )
            await process.stdin.drain()
            response = await self._stdio_exchange(
                process, self._request("tools/call", {"name": tool_name, "arguments": arguments})
            )
            return self._unwrap(response)
        finally:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()

    async def _stdio_exchange(
        self, process: asyncio.subprocess.Process, request: dict[str, Any]
    ) -> dict[str, Any]:
        assert process.stdin and process.stdout
        process.stdin.write((json.dumps(request) + "\n").encode())
        await process.stdin.drain()
        request_id = request["id"]
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), 120)
            if not line:
                raise MCPError("MCP stdio server closed unexpectedly")
            message = json.loads(line)
            if message.get("id") == request_id:
                return message

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}

    @staticmethod
    def _unwrap(response: dict[str, Any]) -> dict[str, Any]:
        if "error" in response:
            raise MCPError(str(response["error"]))
        return response.get("result", response)
