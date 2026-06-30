"""Agent 核心流程：编排 LLM 调用和结果处理"""

import json
import time
from .git_utils import get_full_diff, GitCommandError
from .llm_client import LLMClient
from .prompts import SYSTEM_PROMPT, build_diff_prompt

# 避免单次请求发送过大的 diff
MAX_DIFF_CHARS = 8_000
# API 重试配置
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5


def _truncate_diff(diff: str, max_chars: int = MAX_DIFF_CHARS) -> str:
    """截断过长的 diff，避免超出 LLM 上下文窗口。"""
    if len(diff) <= max_chars:
        return diff
    return (
        diff[:max_chars]
        + f"\n\n... (diff 过长，已截断，仅展示前 {max_chars} 字符)"
    )


def _is_transient_error(exception: Exception) -> bool:
    """判断是否为可重试的瞬态错误（网络超时、连接中断等）。"""
    name = type(exception).__name__
    return any(
        keyword in name.lower()
        for keyword in ("timeout", "connection", "rate", "server", "retry", "network")
    )


def generate_commit_message(
    api_key: str,
    model: str = "deepseek-chat",
) -> dict:
    """
    Agent 主流程：
    1. 读取 git diff
    2. 调 DeepSeek API（带工具定义）
    3. 解析响应，提取结构化的 commit message
    4. 返回 dict 或 error dict

    返回格式（成功）：
    {"type": "feat", "title": "feat: xxx", "body": "..."}
    返回格式（失败）：
    {"error": "原因"}
    """
    # Step 1: 获取 diff
    try:
        diff = get_full_diff()
    except GitCommandError as e:
        return {"error": str(e)}

    if not diff.strip():
        return {"error": "没有检测到代码变更，请先修改代码后再运行"}

    # 截断过长的 diff
    diff = _truncate_diff(diff)

    # Step 2: 初始化客户端，发送请求（带重试）
    client = LLMClient(api_key=api_key, model=model)
    user_message = build_diff_prompt(diff)

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.send_with_tools(SYSTEM_PROMPT, user_message)
            break
        except Exception as e:
            last_error = e
            if attempt == MAX_RETRIES or not _is_transient_error(e):
                return {"error": f"API 调用失败: {e}"}
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    # Step 3: 解析响应，提取工具调用结果
    message = response.choices[0].message

    if message.tool_calls:
        tool_call = message.tool_calls[0]
        if tool_call.function.name == "generate_commit_message":
            try:
                args = json.loads(tool_call.function.arguments)
            except (json.JSONDecodeError, TypeError) as e:
                return {
                    "error": f"LLM 返回格式异常，请重试。"
                    f" 原始内容: {tool_call.function.arguments[:200]}"
                }
            try:
                return {
                    "type": args.get("type", "unknown"),
                    "title": args["title"],
                    "body": args.get("body", ""),
                }
            except KeyError:
                return {
                    "error": f"LLM 返回缺少必填字段 title。"
                    f" 收到: {json.dumps(args, ensure_ascii=False)[:200]}"
                }

    # 如果 LLM 直接返回文本（没有调工具）
    if message.content:
        return {"error": message.content}

    return {"error": "未能生成 commit message"}
