#!/usr/bin/env python3
"""Small loopback-only proxy for viewing applications from several dev data roots.

The Studio normally targets one platform backend. This proxy aggregates
``GET /api/v1/applications`` and routes application-specific requests back to
the backend that owns the application. It is intentionally a development
convenience, not a production project registry.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from typing import Any


UUID_RE = re.compile(
    r"(?<![0-9a-f])"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"(?![0-9a-f])",
    re.IGNORECASE,
)
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


@dataclass(frozen=True, slots=True)
class Backend:
    label: str
    url: str
    token: str
    application_ids: frozenset[str]


def parse_backends(raw: str) -> list[Backend]:
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("--backends must be valid JSON") from error
    if not isinstance(values, list) or not values:
        raise ValueError("--backends must contain at least one backend")

    result: list[Backend] = []
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise ValueError(f"backend {index} must be an object")
        label = str(value.get("label", "")).strip()
        url = str(value.get("url", "")).rstrip("/")
        token = str(value.get("token", ""))
        parsed = urllib.parse.urlparse(url)
        if (
            not label
            or parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not parsed.port
            or not token
        ):
            raise ValueError(
                f"backend {index} needs a label, loopback HTTP URL, and token"
            )
        raw_ids = value.get("application_ids", [])
        if not isinstance(raw_ids, list):
            raise ValueError(f"backend {index} application_ids must be a list")
        application_ids = frozenset(str(item).lower() for item in raw_ids)
        result.append(
            Backend(
                label=label,
                url=url,
                token=token,
                application_ids=application_ids,
            )
        )
    return result


class MultiPlatformProxy:
    def __init__(self, backends: list[Backend], access_token: str) -> None:
        self.backends = backends
        self.default_backend = backends[-1]
        self.access_token = access_token
        self._application_backends: dict[str, Backend] = {}
        self._lock = Lock()

    @staticmethod
    def _backend_request(
        backend: Backend,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        accept: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        headers = {"Authorization": f"Bearer {backend.token}"}
        if content_type:
            headers["Content-Type"] = content_type
        if accept:
            headers["Accept"] = accept
        request = urllib.request.Request(
            backend.url + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return (
                    response.status,
                    dict(response.headers.items()),
                    response.read(),
                )
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers.items()), error.read()

    def _list_backend_applications(
        self, backend: Backend
    ) -> tuple[list[dict[str, Any]], str | None]:
        try:
            status, _, body = self._backend_request(
                backend, "GET", "/api/v1/applications"
            )
        except (OSError, urllib.error.URLError) as error:
            return [], str(error)
        if status != 200:
            return [], f"HTTP {status}"
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return [], "invalid JSON"
        if not isinstance(payload, list):
            return [], "unexpected response"
        applications = [item for item in payload if isinstance(item, dict)]
        if backend.application_ids:
            applications = [
                item
                for item in applications
                if str(item.get("id", "")).lower() in backend.application_ids
            ]
        return applications, None

    def applications(self) -> tuple[int, dict[str, str], bytes]:
        combined: list[dict[str, Any]] = []
        mapping: dict[str, Backend] = {}
        unavailable: list[str] = []
        for backend in self.backends:
            applications, error = self._list_backend_applications(backend)
            if error:
                unavailable.append(f"{backend.label}: {error}")
                continue
            for application in applications:
                application_id = str(application.get("id", "")).lower()
                if not application_id or application_id in mapping:
                    continue
                mapping[application_id] = backend
                item = dict(application)
                original_name = str(item.get("name", "")).strip() or "未命名工作流"
                item["name"] = f"[{backend.label}] {original_name}"
                combined.append(item)
        with self._lock:
            self._application_backends = mapping
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if unavailable:
            headers["X-Lilies-Unavailable-Backends"] = "; ".join(unavailable)
        return 200, headers, json.dumps(combined, ensure_ascii=False).encode()

    def _backend_for_path(self, path: str) -> Backend:
        parsed = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qs(parsed.query)
        candidates = list(UUID_RE.findall(parsed.path))
        candidates.extend(query.get("application_id", []))
        with self._lock:
            mapping = dict(self._application_backends)
        for candidate in candidates:
            backend = mapping.get(candidate.lower())
            if backend is not None:
                return backend
        if candidates:
            self.applications()
            with self._lock:
                for candidate in candidates:
                    backend = self._application_backends.get(candidate.lower())
                    if backend is not None:
                        return backend
        return self.default_backend

    def forward(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None,
        content_type: str | None,
        accept: str | None,
    ) -> tuple[int, dict[str, str], bytes]:
        if method == "GET" and urllib.parse.urlsplit(path).path == "/api/v1/applications":
            return self.applications()
        backend = self._backend_for_path(path)
        return self._backend_request(
            backend,
            method,
            path,
            body=body,
            content_type=content_type,
            accept=accept,
        )


class ProxyHandler(BaseHTTPRequestHandler):
    server: "ProxyServer"

    def do_GET(self) -> None:
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def do_PUT(self) -> None:
        self._proxy()

    def do_PATCH(self) -> None:
        self._proxy()

    def do_DELETE(self) -> None:
        self._proxy()

    def _proxy(self) -> None:
        expected = f"Bearer {self.server.proxy.access_token}"
        if self.headers.get("Authorization") != expected:
            payload = json.dumps({"detail": "invalid API token"}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length else None
        try:
            status, headers, payload = self.server.proxy.forward(
                self.command,
                self.path,
                body=body,
                content_type=self.headers.get("Content-Type"),
                accept=self.headers.get("Accept"),
            )
        except (OSError, urllib.error.URLError) as error:
            status = 502
            headers = {"Content-Type": "application/json"}
            payload = json.dumps(
                {"detail": f"development backend unavailable: {error}"}
            ).encode()

        self.send_response(status)
        for name, value in headers.items():
            if name.lower() in HOP_BY_HOP_HEADERS or name.lower() == "content-length":
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


class ProxyServer(ThreadingHTTPServer):
    def __init__(
        self, address: tuple[str, int], proxy: MultiPlatformProxy
    ) -> None:
        super().__init__(address, ProxyHandler)
        self.proxy = proxy


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aggregate several loopback Lilies platform backends for Studio."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8029, type=int)
    parser.add_argument(
        "--token",
        default=os.environ.get("LILIES_DEV_PROXY_TOKEN", ""),
        help="proxy access token (or LILIES_DEV_PROXY_TOKEN)",
    )
    parser.add_argument(
        "--backends",
        default=os.environ.get("LILIES_DEV_PLATFORM_BACKENDS", ""),
        help="backend JSON (or LILIES_DEV_PLATFORM_BACKENDS)",
    )
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        parser.error("--host must be loopback")
    if not args.token:
        parser.error("--token or LILIES_DEV_PROXY_TOKEN is required")
    if not args.backends:
        parser.error("--backends or LILIES_DEV_PLATFORM_BACKENDS is required")
    proxy = MultiPlatformProxy(parse_backends(args.backends), args.token)
    proxy.applications()
    server = ProxyServer((args.host, args.port), proxy)
    print(f"Lilies development project proxy: http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
