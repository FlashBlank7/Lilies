"""全局失败告警 webhook：发得出、关得掉、坏地址不炸。"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from agent_platform.config import get_settings
from agent_platform.workflow_runtime import WorkflowRuntime

received: list[dict] = []


class Hook(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        received.append(json.loads(self.rfile.read(length)))
        self.send_response(200)
        self.end_headers()


def _fake_self(app_id="app-9"):
    async def get_run(run_id):
        return {"application_id": app_id}

    return SimpleNamespace(workflow_store=SimpleNamespace(get_run=get_run))


def _state():
    return SimpleNamespace(run_id="r-1", snapshot=SimpleNamespace(name="GPU日报"))


def test_alert_fires_with_payload(monkeypatch):
    received.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv(
            "ALERT_WEBHOOK_URL", f"http://127.0.0.1:{server.server_address[1]}/alert")
        get_settings.cache_clear()
        asyncio.run(WorkflowRuntime._alert_run_failed(_fake_self(), _state(), "boom" * 200))
        assert received, "webhook 未收到"
        body = received[0]
        assert body["kind"] == "run_failed"
        assert body["workflow"] == "GPU日报"
        assert body["run_id"] == "r-1"
        assert body["application_id"] == "app-9"
        assert len(body["error"]) == 500  # 截断
    finally:
        server.shutdown()
        server.server_close()
        monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
        get_settings.cache_clear()


def test_disabled_is_noop(monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    get_settings.cache_clear()
    received.clear()
    asyncio.run(WorkflowRuntime._alert_run_failed(_fake_self(), _state(), "boom"))
    assert not received


def test_bad_url_never_raises(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://127.0.0.1:1/nope")
    get_settings.cache_clear()
    try:
        asyncio.run(WorkflowRuntime._alert_run_failed(_fake_self(), _state(), "boom"))
    finally:
        monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
        get_settings.cache_clear()
