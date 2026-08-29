"""注进去的密钥真值，不能出现在 tool.started 事件里。

真机复现（2026-08-29）。runtime 里那段的顺序原来是：

    tool_input = await harness.inject_secret_references(...)   # 真值注进来
    await emit("tool.started", {"input": self._redact(tool_input)})

而那个脱敏器**只看键名**。于是：

    {"api_key": {"$secret": "api_token"}}  → {"api_key": "***"}          挡住
    {"url":  {"$secret": "api_token", "prefix": "https://x/?k="}}
                                          → {"url": "https://x/?k=sk-REALSECRET…"}  漏
    {"note": {"$secret": "api_token"}}     → {"note": "sk-REALSECRET…"}            漏

事件是要落盘、要展示的。

两处一起改（"闸要铺满所有出口"）：
1. 事件报**注入之前**那份，也就是还写着 {"$secret": …} 的那份——
   真值压根不进事件；留下的是"这儿引用了一个密钥"的形状。
2. redact_sensitive_fields 补上值形状匹配。横着比过平台里的几个脱敏器：
   同一批毒载荷，build_transcript.redact 六条全挡，它只挡三条。
   弱的那个正好挂在事件上，所以补齐；正则和那份保持一致。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_platform.agent_core import redact_sensitive_fields
from agent_platform.build_transcript import redact as transcript_redact
from agent_platform.platform_harness import PlatformHarness
from agent_platform.runtime import AgentRuntime
from agent_platform.storage import Storage

SECRET = "sk-REALSECRET0123456789"

POISON = [
    ("敏感键名", {"api_key": "abc1234567890123"}),
    ("键名无辜、值是密钥", {"note": "用这个 sk-ABCDEFGHIJKLMNOPQRSTUV"}),
    ("嵌套一层里的敏感键", {"outer": {"password": "hunter2"}}),
    ("列表里的敏感键", {"items": [{"token": "t-123456789012345678"}]}),
    ("Bearer 头", {"h": "Authorization: Bearer ABCDEFGHIJKLMNOPQRST"}),
    ("裸字符串里的密钥", "泄漏了 sk-ZZZZZZZZZZZZZZZZZZZZ 这个"),
]


@pytest.fixture
def harness(tmp_path: Path):
    storage = Storage(tmp_path / "s")
    asyncio.run(storage.initialize())
    made = PlatformHarness(storage=storage, secret_envelope_key="k",
                           secret_envelope_key_id="kid")
    asyncio.run(made.save_secret(owner_id="own", name="api_token",
                                 value=SECRET, description=""))
    return made


class TestTheRealValueNeverReachesAnEvent:
    """照 runtime 里的顺序走一遍：注入，然后按它现在报的那份脱敏。"""

    @pytest.mark.parametrize("key", ["api_key", "url", "note", "body"])
    def test_no_field_name_lets_the_secret_through(self, harness, key):
        asked = {key: {"$secret": "api_token"}}
        injected = asyncio.run(harness.inject_secret_references(
            owner_id="own", payload=asked, allow_secret_references=True))
        assert SECRET in str(injected), "前提：真值确实被注进去了"

        # runtime 现在报的是**注入之前**那份
        announced = AgentRuntime._redact(asked)
        assert SECRET not in str(announced), announced
        # 留下的是"这儿引用了一个密钥"的形状，不是真值。
        # 密钥的**名字**也被盖掉了（`$secret` 这个键本身带 "secret"）——
        # 顺手写测试时我以为名字会留着，实测不留。
        # 不为这个去给脱敏器开口子：少露一点没坏处，改规则才有风险。
        assert "***" in str(announced), announced
        assert "$secret" in str(announced) or key == "api_key", announced

    def test_a_prefix_does_not_smuggle_it_out(self, harness):
        """带 prefix 的那种正是真机上漏出来的形状。"""
        asked = {"url": {"$secret": "api_token", "prefix": "https://x/?k="}}
        injected = asyncio.run(harness.inject_secret_references(
            owner_id="own", payload=asked, allow_secret_references=True))
        assert f"https://x/?k={SECRET}" in str(injected), "前提：真值确实拼进去了"
        assert SECRET not in str(AgentRuntime._redact(asked))

    def test_the_tool_still_gets_the_real_value(self, harness):
        """反向那一条：别为了不漏就把工具也饿着。

        少了它，"事件里没有真值"可以靠"根本不注入"实现——
        那是把功能删了，不是把洞补了。
        """
        injected = asyncio.run(harness.inject_secret_references(
            owner_id="own", payload={"url": {"$secret": "api_token"}},
            allow_secret_references=True))
        assert injected == {"url": SECRET}


class TestTheEventRedactorIsAsStrongAsTheTranscriptOne:
    """两个脱敏器强弱可以不同，但都不该漏这六类。"""

    @pytest.mark.parametrize("label, payload", POISON,
                             ids=[label for label, _ in POISON])
    def test_neither_redactor_leaks(self, label, payload):
        for name, fn in (("事件", redact_sensitive_fields),
                         ("transcript", transcript_redact)):
            out = str(fn(payload))
            for leak in ("abc1234567890123", "sk-ABCDEF", "hunter2",
                         "t-1234567890", "Bearer ABCDEF", "sk-ZZZZ"):
                assert leak not in out, f"{name} 漏了 {label}：{out}"

    def test_ordinary_text_is_left_alone(self):
        """别把正常内容也打成 ***——脱敏过头会把诊断逼进数据库。

        （usage 里那些 *_tokens 字段被误伤过一次，是已知缺陷 #6 的由来。）
        """
        plain = {"title": "词频统计", "note": "跑了 12 次", "count": 12}
        assert redact_sensitive_fields(plain) == plain


class TestTheRealRuntimePathIsCovered:
    """上面那些是拿 _redact 单独试的——那只验了脱敏器，没验**顺序**。

    实测（变异验证）：把 runtime 里的 `self._redact(announced)` 改回
    `self._redact(tool_input)`，上面 13 条一条不红。所以必须真跑一遍
    runtime 的取回路，从落盘的事件里查。

    这也是今天反复撞的那件事：测试重搭了一遍被测代码的调用方式，
    于是测的是我重搭的那份，不是线上那份。
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("secret", [
        SECRET,
        # **不长得像密钥的密钥**——数据库口令、basic auth 之类，
        # 值形状正则够不着它。这一条是唯一只靠"报注入前那份"守住的：
        # 只把顺序改回去、脱敏器留强的，上一条照样绿（实测），这一条会红。
        "correct-horse-battery-staple",
    ], ids=["长得像密钥", "不长得像密钥"])
    async def test_the_stored_event_has_no_secret(self, tmp_path, secret):
        from typing import Any, AsyncIterator

        from agent_platform.config import Settings
        from agent_platform.models import (
            AgentSpec, ChatMessage, PermissionMode, StreamEvent, ToolDefinition,
        )
        from agent_platform.permissions import PermissionBroker
        from agent_platform.providers.base import ModelProvider, ProviderCapabilities
        from agent_platform.tools import build_core_registry

        from tests.test_runtime import FakeSandboxes

        class WritesASecret(ModelProvider):
            """第一轮拿 {"$secret": …} 调 Write，第二轮收工。"""

            name = "scripted"

            def __init__(self) -> None:
                self.calls = 0

            def capabilities(self, model: str) -> ProviderCapabilities:
                return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

            async def stream(self, **_: Any) -> AsyncIterator[StreamEvent]:
                self.calls += 1
                yield StreamEvent(type="message_start",
                                  data={"message": {"usage": {"input_tokens": 1}}})
                if self.calls == 1:
                    yield StreamEvent(type="content_block_start", data={
                        "index": 0,
                        "content_block": {"type": "tool_use", "id": "t1",
                                          "name": "Write", "input": {}}})
                    yield StreamEvent(type="content_block_delta", data={
                        "index": 0,
                        "delta": {"type": "input_json_delta",
                                  "partial_json": '{"path":"a.txt","content":'
                                                  '{"$secret":"api_token"}}'}})
                    yield StreamEvent(type="content_block_stop", data={"index": 0})
                    yield StreamEvent(type="message_delta",
                                      data={"delta": {"stop_reason": "tool_use"}})
                else:
                    yield StreamEvent(type="content_block_start", data={
                        "index": 0, "content_block": {"type": "text", "text": ""}})
                    yield StreamEvent(type="content_block_delta", data={
                        "index": 0, "delta": {"type": "text_delta", "text": "完成"}})
                    yield StreamEvent(type="content_block_stop", data={"index": 0})
                    yield StreamEvent(type="message_delta",
                                      data={"delta": {"stop_reason": "end_turn"}})

        settings = Settings(data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
        settings.prepare()
        storage = Storage(settings.data_dir)
        await storage.initialize()
        harness = PlatformHarness(storage=storage, secret_envelope_key="k",
                                  secret_envelope_key_id="kid")
        spec = AgentSpec(name="w", description="写文件的智能体",
                         system_prompt="用给你的工具把文件写出来，写完回一句「完成」就行。",
                         tools=["Write"], permission_mode=PermissionMode.bypass)
        version = await storage.save_agent_version(spec, "published")
        runtime = AgentRuntime(
            settings=settings, storage=storage, provider=WritesASecret(),
            tools=build_core_registry(),
            sandboxes=FakeSandboxes(settings.workspace_root),  # type: ignore[arg-type]
            permissions=PermissionBroker(), harness=harness,
        )
        session = await runtime.create_session(spec, version, ".")
        await harness.save_secret(owner_id=spec.id, name="api_token",
                                  value=secret, description="")
        await runtime.run_turn_and_wait(session, "写一个文件")

        events = await storage.list_events(session.id)
        started = [e for e in events if e.type == "tool.started"]
        assert started, "前提：这一轮确实调了工具"
        assert secret not in str([e.data for e in started]), started[0].data
        assert secret not in str([e.data for e in events]), "别的事件里也不许有"
