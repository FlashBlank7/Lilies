"""Platform agent loop: model inference, tool execution and persisted conversation."""
from __future__ import annotations

import asyncio
import json
import time
from uuid import uuid4

from .agent_core import collect_model_stream
from .models import ChatMessage, ContentBlock, ToolDefinition


class ModelSession:
    def __init__(self, provider, runtime_dir, *, max_model_calls=32, max_output_tokens=8192):
        self.provider, self.runtime_dir = provider, runtime_dir
        self.max_model_calls, self.max_output_tokens = max_model_calls, max_output_tokens
        self.model_calls = 0
        self.thread_id = None
        self.turn_id = None
        self.pending = []
        self.messages = []

    async def start(self, tools, instructions, thread_id=None):
        self.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.runtime_dir / "conversation.json"
        self.tools = [ToolDefinition(name=t["name"], description=t["description"], input_schema=t["inputSchema"]) for t in tools]
        self.instructions = instructions
        self.thread_id = thread_id or str(uuid4())
        if thread_id:
            if not self.path.is_file():
                raise ValueError("项目模型会话文件缺失，请恢复会话文件后继续")
            saved = json.loads(self.path.read_text())
            if saved["thread_id"] != thread_id:
                raise ValueError("项目模型会话编号不匹配")
            self.messages = [ChatMessage.model_validate(m) for m in saved["messages"]]
            self.pending = saved.get("pending", [])
        self.save()
        return self.thread_id

    def save(self):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"thread_id": self.thread_id,
            "pending": self.pending,
            "messages": [m.model_dump(mode="json", exclude_none=True) for m in self.messages]}, ensure_ascii=False))
        temp.chmod(0o600)
        temp.replace(self.path)

    def reset_budget(self):
        """Only a new user request resets the allowance, not background results."""
        self.model_calls = 0

    async def turn(self, message, on_event, on_tool, *, timeout=900):
        self.turn_id = str(uuid4())
        if self.pending:
            self.messages.append(ChatMessage(role="user", content=[ContentBlock(type="text", text="\n".join(self.pending))]))
            self.pending.clear()
        self.messages.append(ChatMessage(role="user", content=[ContentBlock(type="text", text=message)]))
        self.save()
        try:
            while self.model_calls < self.max_model_calls:
                if self.pending:
                    self.messages.append(ChatMessage(role="user", content=[ContentBlock(type="text", text="\n".join(self.pending))]))
                    self.pending.clear()
                    self.save()
                started = time.perf_counter()
                self.model_calls += 1
                response = await collect_model_stream(self.provider.stream(model="project", system=self.instructions,
                    messages=self.messages, tools=self.tools, max_output_tokens=self.max_output_tokens,
                    thinking_enabled=True, effort="medium"), timeout_seconds=timeout, expose_thinking=True)
                await on_event('model_usage', {'usage': response.usage.model_dump(mode='json'),
                    'seconds': time.perf_counter() - started})
                self.messages.append(ChatMessage(role="assistant", content=response.blocks))
                calls = [b for b in response.blocks if b.type == "tool_use"]
                text = "".join(b.text or "" for b in response.blocks if b.type == "text")
                if calls:
                    results = ChatMessage(role="user", content=[ContentBlock(type="tool_result", tool_use_id=call.id,
                        is_error=True, content="本次工具未执行，会话已中断") for call in calls])
                    self.messages.append(results)
                # Persist the requests before any side effects, with a complete
                # result message so even an abrupt restart can resume the thread.
                self.save()
                if text:
                    await on_event("item/completed", {"item": {"type": "agentMessage", "text": text}})
                cancelled = False
                for index, call in enumerate(calls):
                    try:
                        if cancelled:
                            raise RuntimeError("用户已停止，本次工具未执行")
                        if call.name not in {t.name for t in self.tools}:
                            raise ValueError("未知项目工具")
                        results.content[index] = ContentBlock(type="tool_result", tool_use_id=call.id, is_error=True,
                            content="工具执行结果未确认；请先读取项目状态核对结果，不要盲目重复写入")
                        self.save()
                        value = await on_tool(call.name, call.input or {})
                        results.content[index] = ContentBlock(type="tool_result", tool_use_id=call.id, content=json.dumps(value, ensure_ascii=False))
                    except asyncio.CancelledError:
                        cancelled = True
                        results.content[index] = ContentBlock(type="tool_result", tool_use_id=call.id, is_error=True, content="操作被停止；请先读取项目状态核对结果，不要盲目重复写入")
                    except Exception as error:
                        results.content[index] = ContentBlock(type="tool_result", tool_use_id=call.id, is_error=True, content=str(error))
                    self.save()
                if cancelled:
                    raise asyncio.CancelledError
                if not calls and not self.pending:
                    if response.stop_reason == "max_tokens":
                        raise RuntimeError("模型输出达到长度限制，请压缩本轮任务后继续")
                    return {"status": "completed"}
            raise RuntimeError(f"本次请求已达到 {self.max_model_calls} 次对话模型调用上限，进展已保留；需要继续时请发送新消息")
        finally:
            self.turn_id = None

    async def steer(self, message):
        self.pending.append(message)
        self.save()

    async def close(self):
        pass  # subprocess and HTTP lifetimes belong to the cancelled turn coroutine
