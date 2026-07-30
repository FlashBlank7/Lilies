#!/usr/bin/env python3
"""Small generic HTTP fault proxy for Connector acceptance tests."""

from __future__ import annotations

import argparse
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FaultState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.mode = "pass"
        self.remaining = 0

    def configure(self, mode: str, count: int) -> None:
        if mode not in {"pass", "transient_503", "permission_403"}:
            raise ValueError("unsupported fault mode")
        with self.lock:
            self.mode = mode
            self.remaining = max(0, count)

    def response_for(self, method: str, path: str) -> int | None:
        if method != "POST" or path.split("?", 1)[0] != "/api/alarm":
            return None
        with self.lock:
            if self.mode == "permission_403":
                return 403
            if self.mode == "transient_503" and self.remaining > 0:
                self.remaining -= 1
                return 503
        return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: FaultState
    target_host: str
    target_port: int

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length", "0")
        return self.rfile.read(int(raw_length)) if raw_length else b""

    def _json(
        self,
        status: int,
        value: Any,
        *,
        pre_dispatch: bool = False,
    ) -> None:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if pre_dispatch:
            self.send_header(
                "X-Lilies-Pre-Dispatch",
                "exp004-fault-proxy-v1",
            )
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle(self) -> None:
        body = self._read_body()
        if self.path == "/__fault__/configure":
            try:
                request = json.loads(body or b"{}")
                self.state.configure(
                    str(request.get("mode", "pass")),
                    int(request.get("count", 0)),
                )
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self._json(400, {"error": str(error)})
                return
            self._json(200, {"status": "configured"})
            return

        injected = self.state.response_for(self.command, self.path)
        if injected is not None:
            self._json(
                injected,
                {
                    "status": injected,
                    "message": (
                        "temporary upstream failure"
                        if injected == 503
                        else "permission denied"
                    ),
                },
                pre_dispatch=injected == 503,
            )
            return

        forwarded_headers = {
            key: value
            for key, value in self.headers.items()
            if key.casefold()
            not in {
                "connection",
                "content-length",
                "host",
                "proxy-connection",
                "transfer-encoding",
            }
        }
        connection = http.client.HTTPConnection(
            self.target_host,
            self.target_port,
            timeout=60,
        )
        try:
            connection.request(
                self.command,
                self.path,
                body=body or None,
                headers=forwarded_headers,
            )
            response = connection.getresponse()
            payload = response.read()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.casefold() not in {
                    "connection",
                    "content-length",
                    "transfer-encoding",
                }:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        finally:
            connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19091)
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, default=19090)
    args = parser.parse_args()

    state = FaultState()
    handler = type(
        "ConfiguredHandler",
        (Handler,),
        {
            "state": state,
            "target_host": args.target_host,
            "target_port": args.target_port,
        },
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
