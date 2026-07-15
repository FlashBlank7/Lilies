from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts.v04_03_browser_environment import (
    ROOT,
    commands,
    prepare_standalone_assets,
    probe,
    wait_until_ready,
)


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: object) -> None:
        return


def test_prepare_standalone_assets_copies_static_and_public(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    server = frontend / ".next/standalone/server.js"
    server.parent.mkdir(parents=True)
    server.write_text("server", encoding="utf-8")
    static = frontend / ".next/static/chunks/app.js"
    static.parent.mkdir(parents=True)
    static.write_text("chunk", encoding="utf-8")
    public = frontend / "public/icon.txt"
    public.parent.mkdir(parents=True)
    public.write_text("icon", encoding="utf-8")

    assert prepare_standalone_assets(frontend) == server
    assert (frontend / ".next/standalone/.next/static/chunks/app.js").read_text() == "chunk"
    assert (frontend / ".next/standalone/public/icon.txt").read_text() == "icon"

    with pytest.raises(FileNotFoundError, match="standalone server"):
        prepare_standalone_assets(tmp_path / "missing")


def test_probe_and_wait_until_ready_use_real_http_status() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        assert probe(url)["status"] == 200
        assert wait_until_ready([url], timeout=1)[0]["ready"] is True
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_environment_commands_are_isolated_from_user_ports() -> None:
    backend, frontend = commands(
        node=Path("/tmp/node"),
        standalone_server=Path("/tmp/server.js"),
        api_host="127.0.0.1",
        api_port=8002,
    )

    assert backend[:3] == [str(ROOT / ".venv/bin/python"), "-m", "uvicorn"]
    assert backend[-2:] == ["--port", "8002"]
    assert frontend == ["/tmp/node", "/tmp/server.js"]
