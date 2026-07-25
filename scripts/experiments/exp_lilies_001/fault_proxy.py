from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_REQUEST_LOG_ENTRIES = 10_000
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_REQUEST_HEADERS = frozenset(
    {
        "accept",
        "accept-language",
        "authorization",
        "content-type",
        "idempotency-key",
        "x-api-version",
    }
)
_RESPONSE_HEADERS = frozenset(
    {
        "content-disposition",
        "content-language",
        "content-type",
        "etag",
        "last-modified",
        "location",
        "retry-after",
        "x-api-version",
        "x-version",
    }
)


class FaultProxyError(RuntimeError):
    """The bounded experiment proxy rejected an unsafe request or state."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


@contextmanager
def _locked_state(path: Path) -> Iterator[tuple[Any, dict[str, Any]]]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        raw = handle.read()
        value = (
            json.loads(raw)
            if raw
            else {
                "schema_version": "1.0",
                "active": False,
                "transient_source_ids": [],
                "permission_source_ids": [],
                "consumed_transient_source_ids": [],
                "all_source_ids": [],
                "request_log": [],
            }
        )
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != "1.0"
            or not isinstance(value.get("active"), bool)
            or any(
                not isinstance(value.get(key), list)
                or any(
                    not isinstance(item, str) or not item
                    for item in value[key]
                )
                for key in (
                    "transient_source_ids",
                    "permission_source_ids",
                    "consumed_transient_source_ids",
                )
            )
        ):
            raise FaultProxyError("fault state has an invalid schema")
        value.setdefault("all_source_ids", [])
        value.setdefault("request_log", [])
        if (
            not isinstance(value["all_source_ids"], list)
            or any(
                not isinstance(item, str) or not item
                for item in value["all_source_ids"]
            )
            or not isinstance(value["request_log"], list)
            or any(not isinstance(item, dict) for item in value["request_log"])
            or len(value["request_log"]) > MAX_REQUEST_LOG_ENTRIES
        ):
            raise FaultProxyError("fault request log has an invalid schema")
        yield handle, value
        handle.seek(0)
        handle.truncate()
        json.dump(
            value,
            handle,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        handle.flush()
        os.fsync(handle.fileno())


def _fault_status(
    state_path: Path,
    *,
    method: str,
    request_text: str,
) -> int | None:
    if method not in _MUTATING_METHODS:
        return None
    with _locked_state(state_path) as (_handle, state):
        if not state["active"]:
            return None
        for source_id in state["permission_source_ids"]:
            if source_id in request_text:
                return 403
        consumed = set(state["consumed_transient_source_ids"])
        for source_id in state["transient_source_ids"]:
            if source_id in request_text and source_id not in consumed:
                state["consumed_transient_source_ids"].append(source_id)
                state["consumed_transient_source_ids"].sort()
                return 503
    return None


def _record_request(
    state_path: Path,
    *,
    proxy_id: str,
    method: str,
    path: str,
    request_body: bytes,
    response_body: bytes,
    status: int,
    injected: bool,
) -> None:
    if method not in _MUTATING_METHODS:
        return
    request_text = path + "\n" + request_body.decode("utf-8", errors="ignore")
    with _locked_state(state_path) as (_handle, state):
        log = state["request_log"]
        if len(log) >= MAX_REQUEST_LOG_ENTRIES:
            raise FaultProxyError("fault proxy request log is full")
        source_ids = sorted(
            source_id
            for source_id in state["all_source_ids"]
            if source_id in request_text
        )
        log.append(
            {
                "sequence": len(log) + 1,
                "proxy": proxy_id,
                "method": method,
                "path": path,
                "source_ids": source_ids,
                "status": status,
                "injected": injected,
                "request_digest": (
                    f"sha256:{hashlib.sha256(request_body).hexdigest()}"
                ),
                "response_digest": (
                    f"sha256:{hashlib.sha256(response_body).hexdigest()}"
                ),
            }
        )


class _Handler(BaseHTTPRequestHandler):
    server_version = "LiliesExperimentFaultProxy/1.0"

    def _handle(self) -> None:
        if (
            not self.path.startswith("/api/")
            or "://" in self.path
            or "\x00" in self.path
        ):
            self.send_error(404)
            return
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length < 0 or content_length > MAX_BODY_BYTES:
            self.send_error(413)
            return
        body = self.rfile.read(content_length) if content_length else b""
        request_text = self.path + "\n" + body.decode("utf-8", errors="ignore")
        injected = _fault_status(
            self.server.state_path,  # type: ignore[attr-defined]
            method=self.command,
            request_text=request_text,
        )
        if injected is not None:
            payload = json.dumps(
                {
                    "detail": (
                        "injected temporary upstream failure"
                        if injected == 503
                        else "injected permission denial"
                    ),
                    "experiment_fault": True,
                },
                separators=(",", ":"),
            ).encode()
            self.send_response(injected)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if injected == 503:
                self.send_header("Retry-After", "1")
            self.end_headers()
            self.wfile.write(payload)
            _record_request(
                self.server.state_path,  # type: ignore[attr-defined]
                proxy_id=self.server.proxy_id,  # type: ignore[attr-defined]
                method=self.command,
                path=self.path,
                request_body=body,
                response_body=payload,
                status=injected,
                injected=True,
            )
            return
        target = self.server.upstream + self.path  # type: ignore[attr-defined]
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.casefold() in _REQUEST_HEADERS
        }
        request = Request(
            target,
            data=body if content_length else None,
            method=self.command,
            headers=headers,
        )
        opener = build_opener(_NoRedirect)
        response_status: int
        try:
            with opener.open(request, timeout=60) as response:
                response_body = response.read(MAX_BODY_BYTES + 1)
                if len(response_body) > MAX_BODY_BYTES:
                    raise FaultProxyError("upstream response exceeds its limit")
                response_status = int(response.status)
                self.send_response(response_status)
                for name, value in response.headers.items():
                    if name.casefold() in _RESPONSE_HEADERS:
                        self.send_header(name, value)
        except HTTPError as error:
            response_body = error.read(MAX_BODY_BYTES + 1)
            if len(response_body) > MAX_BODY_BYTES:
                raise FaultProxyError("upstream error response exceeds its limit")
            response_status = int(error.code)
            self.send_response(response_status)
            for name, value in error.headers.items():
                if name.casefold() in _RESPONSE_HEADERS:
                    self.send_header(name, value)
        except (URLError, OSError, TimeoutError, FaultProxyError):
            self.send_error(502)
            return
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)
        _record_request(
            self.server.state_path,  # type: ignore[attr-defined]
            proxy_id=self.server.proxy_id,  # type: ignore[attr-defined]
            method=self.command,
            path=self.path,
            request_body=body,
            response_body=response_body,
            status=response_status,
            injected=False,
        )

    do_DELETE = _handle  # noqa: N815
    do_GET = _handle  # noqa: N815
    do_OPTIONS = _handle  # noqa: N815
    do_PATCH = _handle  # noqa: N815
    do_POST = _handle  # noqa: N815
    do_PUT = _handle  # noqa: N815

    def log_message(self, _format: str, *_args: object) -> None:
        return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one fail-closed EXP-LILIES-001 official-API proxy."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--state-path", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    upstream = args.upstream.rstrip("/")
    if upstream not in {
        "http://127.0.0.1:18000",
        "http://127.0.0.1:18001",
    }:
        raise FaultProxyError("proxy upstream is not an exact experiment host")
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    server.upstream = upstream  # type: ignore[attr-defined]
    server.proxy_id = (
        "paperless" if upstream.endswith(":18000") else "inventree"
    )  # type: ignore[attr-defined]
    server.state_path = args.state_path.resolve()  # type: ignore[attr-defined]
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
