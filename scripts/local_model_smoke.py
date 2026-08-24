"""真线冒烟：OpenAIChatProvider 对本地 vLLM 端点的流翻译验证（文本 + 工具调用）。"""
import asyncio, json, os, sys
sys.path.insert(0, "platform/backend/src")
from agent_platform.agent_core import collect_model_stream
from agent_platform.models import ChatMessage, ContentBlock, ToolDefinition
from agent_platform.providers.openai_chat import OpenAIChatProvider

MODEL = os.environ.get("SMOKE_MODEL", "Qwen/Qwen3.5-4B")
provider = OpenAIChatProvider(api_key="local", base_url="http://127.0.0.1:8001/v1")

async def case_text():
    stream = provider.stream(model=MODEL, system="用一句中文回答。",
        messages=[ChatMessage(role="user", content=[ContentBlock(type="text", text="你是谁？")])],
        tools=[], max_output_tokens=128, thinking_enabled=False, effort="low")
    r = await collect_model_stream(stream, model=MODEL)
    text = " ".join(b.text or "" for b in r.blocks if b.type == "text")
    print("TEXT:", r.stop_reason, "|", text[:80].replace(chr(10), " "))
    assert r.stop_reason == "end_turn" and text.strip(), "text case failed"

async def case_tool():
    tool = ToolDefinition(name="draft_add_node",
        description="向工作流草稿添加一个节点",
        input_schema={"type":"object","properties":{"node":{"type":"object","properties":{
            "id":{"type":"string"},"type":{"type":"string"},"title":{"type":"string"}},
            "required":["id","type"]}},"required":["node"]})
    stream = provider.stream(model=MODEL,
        system="你是工作流配置手。必须调用工具完成任务，不要用文字回答。",
        messages=[ChatMessage(role="user", content=[ContentBlock(type="text",
            text="添加一个 id 为 start、类型为 start、标题为 输入 的节点。")])],
        tools=[tool], max_output_tokens=512, thinking_enabled=False, effort="low",
        tool_choice={"type":"auto"})
    r = await collect_model_stream(stream, model=MODEL)
    calls = [b for b in r.blocks if b.type == "tool_use"]
    print("TOOL:", r.stop_reason, "|", [(c.name, c.input) for c in calls][:1])
    assert calls and calls[0].name == "draft_add_node", "no tool call"
    # 线路层到此已验证（调用发起 + 参数 JSON 装配）。
    # 归因更正（2026-08-23）：此处曾注记"小模型拍平嵌套结构是预期失败模式"，
    # 判别实验证明是错误归因——非流式直连时 4B 输出完整合法的嵌套 JSON，
    # 拍平实为 vLLM 0.8.5 hermes 流式工具解析器丢参数片段（上游 #19056，
    # 修复于 v0.11.0）。平台已改走非流式并加兜底解析。schema 质量仍单独观察
    # 不作冒烟断言，但观察到的畸形要先排除管道故障再记到模型头上。
    node = (calls[0].input or {}).get("node")
    if isinstance(node, dict) and node.get("id") == "start" and node.get("type") == "start":
        print("TOOL-SCHEMA: valid nested structure (one-shot)")
    else:
        print(f"TOOL-SCHEMA: invalid one-shot (boundary would reject & retry): {calls[0].input}")

async def main():
    await case_text()
    await case_tool()
    print("REALWIRE-SMOKE-PASS")

asyncio.run(main())
