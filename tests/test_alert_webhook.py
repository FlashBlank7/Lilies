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


def _fake_self(app_id="app-9", current_name=None):
    async def get_run(run_id):
        return {"application_id": app_id}

    async def get_application(application_id):
        if current_name is None:
            raise KeyError(application_id)
        return {"id": application_id, "name": current_name}

    return SimpleNamespace(workflow_store=SimpleNamespace(
        get_run=get_run, get_application=get_application))


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
        # error 是给人看的。**收口从 110 放宽到 500**（2026-08-30）：
        # 110 是面板一行的宽度，告警不是面板——收件人在手机上，
        # 没有别的地方能看到全文，而这条报错线上最有用的一句常常在末尾。
        # 原文留在 error_raw 里按 500 截断，机器消费方不受影响。
        assert len(body["error"]) <= 500 + 40      # 40 是那句"还有 N 字"的余量
        assert body["error"].endswith("在运行详情里）")
        assert len(body["error_raw"]) == 500
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


def test_alert_prefers_current_application_name(monkeypatch):
    """改过名的工作流：告警里必须是用户界面上看得到的当前名，不是发布快照的旧名。"""
    received.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv(
            "ALERT_WEBHOOK_URL", f"http://127.0.0.1:{server.server_address[1]}/alert")
        get_settings.cache_clear()
        asyncio.run(WorkflowRuntime._alert_run_failed(
            _fake_self(current_name="文本行数与净字数统计"), _state(), "boom"))
        assert received[0]["workflow"] == "文本行数与净字数统计"  # 非快照里的 "GPU日报"
    finally:
        server.shutdown()
        server.server_close()
        monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
        get_settings.cache_clear()


def test_alert_falls_back_to_snapshot_name(monkeypatch):
    """查不到应用行（已删等）：退回快照名，告警照发不炸。"""
    received.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv(
            "ALERT_WEBHOOK_URL", f"http://127.0.0.1:{server.server_address[1]}/alert")
        get_settings.cache_clear()
        asyncio.run(WorkflowRuntime._alert_run_failed(_fake_self(), _state(), "boom"))
        assert received[0]["workflow"] == "GPU日报"
        assert received[0]["application_id"] == "app-9"  # app id 在取名字之前已落好
    finally:
        server.shutdown()
        server.server_close()
        monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
        get_settings.cache_clear()


def test_alert_error_is_human_readable(monkeypatch):
    """告警发到钉钉/飞书/手机上，收件人不在终端前面——更不该是英文原文。

    真机验过（起个接收端真收一次）：以前送出去的是
    node start failed: missing required input: text。
    today 面板与体检早就翻成人话了，就差这一处。
    """
    received.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv(
            "ALERT_WEBHOOK_URL", f"http://127.0.0.1:{server.server_address[1]}/alert")
        get_settings.cache_clear()
        asyncio.run(WorkflowRuntime._alert_run_failed(
            _fake_self(current_name="统计"), _state(),
            "node start failed: missing required input: text"))
        assert received, "webhook 未收到"
        body = received[0]
        assert body["error"] == "缺少必填输入「text」"          # 人看的
        assert "missing required input" in body["error_raw"]   # 机器要的原文还在
    finally:
        server.shutdown()
        server.server_close()
        monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
        get_settings.cache_clear()


def _send(monkeypatch, error: str) -> dict:
    """起个真接收端发一次，返回收到的那份 payload。"""
    received.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv(
            "ALERT_WEBHOOK_URL", f"http://127.0.0.1:{server.server_address[1]}/alert")
        get_settings.cache_clear()
        asyncio.run(WorkflowRuntime._alert_run_failed(
            _fake_self(current_name="统计"), _state(), error))
        assert received, "webhook 未收到"
        return received[0]
    finally:
        server.shutdown()
        server.server_close()
        monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
        get_settings.cache_clear()


def test_a_long_error_says_that_it_was_cut(monkeypatch):
    """截了要说。收件人在手机上，没有别的地方能看到全文。

    真机量过：227 条失败里 4 条超过 500 字（最长 656），
    被砍掉的尾巴恰好是「要么让节点 X 真正产出…要么把引用改成…」——
    整条报错里最能照着做的那一句。不说一声，看到的就是一句半截话。
    """
    body = _send(monkeypatch, "报错正文" + "长" * 900)
    assert "还有" in body["error"], body["error"][-80:]
    assert body["error_truncated"] is True


def test_a_short_error_is_left_alone(monkeypatch):
    """反向：不能给每条告警都缀上一句"还有 N 字"。"""
    body = _send(monkeypatch, "node start failed: missing required input: text")
    assert "还有" not in body["error"]
    assert body["error_truncated"] is False


def test_the_machine_field_stays_parseable(monkeypatch):
    """error_raw 是给机器消费方的，不能往里塞中文说明——
    截没截由 error_truncated 单独说。"""
    body = _send(monkeypatch, "boom " * 300)
    assert "还有" not in body["error_raw"] and "字" not in body["error_raw"]
    assert body["error_raw"].startswith("boom")
    assert body["error_truncated"] is True


def test_the_cut_keeps_the_beginning(monkeypatch):
    """截尾不能截头——开头那句是"是什么毛病"。"""
    body = _send(monkeypatch, "取不到「aggregator」的「by_store」" + "补" * 900)
    assert body["error"].startswith("取不到「aggregator」")
