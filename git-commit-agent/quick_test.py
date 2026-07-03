"""
快速测试脚本 — 无需 pytest，直接运行验证 Agent 核心功能

用法：
  python quick_test.py                  # 仅运行模拟测试（不需要 API Key）
  python quick_test.py --with-api       # 包含真实的 API 调用（需设置 DEEPSEEK_API_KEY）

所有测试通过的输出示例：
  ✓ 测试 1: 提示词模板 — 正常 diff
  ✓ 测试 2: 提示词模板 — 空 diff
  ✓ 测试 3: 工具定义格式 — 10 种类型
  ✓ 测试 4: Agent 解析正常响应
  ...

  结果: N/N 测试通过
"""

import json
import os
import sys
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------- 辅助 ----------
passed = 0
failed = 0


def test(name: str):
    """测试装饰器"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            global passed, failed
            try:
                func(*args, **kwargs)
                print(f"  ✓ {name}")
                passed += 1
            except Exception as e:
                print(f"  ✗ {name}")
                print(f"    原因: {e}")
                failed += 1
        return wrapper
    return decorator


# ---------- 测试用例 ----------

@test("提示词模板 — 正常 diff")
def test_prompt_normal():
    sys.path.insert(0, os.path.dirname(__file__))
    from commit_agent.prompts import build_diff_prompt
    result = build_diff_prompt("--- a/a.py\n+++ b/a.py\n+print('hi')")
    assert "```diff" in result
    assert "print('hi')" in result


@test("提示词模板 — 空 diff")
def test_prompt_empty():
    from commit_agent.prompts import build_diff_prompt
    result = build_diff_prompt("")
    assert "没有检测到任何代码变更" in result


@test("SYSTEM_PROMPT 包含全部 10 种类型")
def test_prompt_all_types():
    from commit_agent.prompts import SYSTEM_PROMPT
    for t in ("feat", "fix", "refactor", "docs", "style",
               "test", "chore", "perf", "ci", "build"):
        assert t in SYSTEM_PROMPT, f"SYSTEM_PROMPT 缺少类型 {t}"


@test("工具定义格式 — 10 种类型 + body 字段")
def test_tool_definition():
    from commit_agent.llm_client import LLMClient
    tools = LLMClient.define_tools()
    assert len(tools) == 1
    t = tools[0]
    assert t["type"] == "function"
    assert t["function"]["name"] == "generate_commit_message"
    params = t["function"]["parameters"]
    assert params["required"] == ["title", "type"]
    enum = params["properties"]["type"]["enum"]
    assert set(enum) == {"feat", "fix", "refactor", "docs", "style",
                          "test", "chore", "perf", "ci", "build"}
    assert "body" in params["properties"]


@test("LLMClient 默认 temperature=0.3")
def test_default_temperature():
    from commit_agent.llm_client import LLMClient
    client = LLMClient(api_key="test-key")
    assert client.temperature == 0.3


@test("Agent 解析正常响应")
def test_agent_normal():
    from commit_agent.agent import generate_commit_message

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_message = MagicMock()
    mock_tool_call = MagicMock()
    mock_tool_call.function.name = "generate_commit_message"
    mock_tool_call.function.arguments = json.dumps({
        "title": "feat: 添加测试功能",
        "type": "feat",
        "body": "这是测试正文"
    })
    mock_message.tool_calls = [mock_tool_call]
    mock_message.content = None
    mock_choice.message = mock_message
    mock_response.choices = [mock_choice]

    with patch("commit_agent.agent.get_full_diff") as mock_diff, \
         patch("commit_agent.agent.LLMClient") as mock_client:
        mock_diff.return_value = "diff --git a/a.py b/a.py\n+print('hi')"
        mock_client.return_value.send_with_tools.return_value = mock_response

        result = generate_commit_message(api_key="test-key")
        assert result["type"] == "feat"
        assert "测试" in result["title"]
        assert result["body"] == "这是测试正文"


@test("Agent 处理空 diff")
def test_agent_empty_diff():
    from commit_agent.agent import generate_commit_message

    with patch("commit_agent.agent.get_full_diff") as mock_diff:
        mock_diff.return_value = ""
        result = generate_commit_message(api_key="test-key")
        assert "error" in result
        assert "没有检测到代码变更" in result["error"]


@test("Agent 处理 API 异常")
def test_agent_api_error():
    from commit_agent.agent import generate_commit_message

    with patch("commit_agent.agent.get_full_diff") as mock_diff, \
         patch("commit_agent.agent.LLMClient") as mock_client:
        mock_diff.return_value = "diff --git a/a.py b/a.py\n+data"
        mock_client.return_value.send_with_tools.side_effect = Exception("Connection failed")

        result = generate_commit_message(api_key="test-key")
        assert "error" in result
        assert "API 调用失败" in result["error"]


@test("Agent 处理非法 JSON（不崩溃）")
def test_agent_malformed_json():
    from commit_agent.agent import generate_commit_message

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_message = MagicMock()
    mock_tool_call = MagicMock()
    mock_tool_call.function.name = "generate_commit_message"
    mock_tool_call.function.arguments = "{title: broken json"  # 非法 JSON
    mock_message.tool_calls = [mock_tool_call]
    mock_message.content = None
    mock_choice.message = mock_message
    mock_response.choices = [mock_choice]

    with patch("commit_agent.agent.get_full_diff") as mock_diff, \
         patch("commit_agent.agent.LLMClient") as mock_client:
        mock_diff.return_value = "diff --git a/a.py b/a.py\n+data"
        mock_client.return_value.send_with_tools.return_value = mock_response

        result = generate_commit_message(api_key="test-key")
        assert "error" in result
        assert "格式异常" in result["error"]


@test("Agent 处理 Git 仓库错误")
def test_agent_not_git_repo():
    from commit_agent.agent import generate_commit_message
    from commit_agent.git_utils import GitCommandError

    with patch("commit_agent.agent.get_full_diff") as mock_diff:
        mock_diff.side_effect = GitCommandError("当前目录不是 Git 仓库")

        result = generate_commit_message(api_key="test-key")
        assert "error" in result
        assert "不是 Git 仓库" in result["error"]


@test("CLI 预览展示正常")
def test_cli_display():
    from commit_agent.cli import display_preview
    result = display_preview({
        "type": "refactor",
        "title": "refactor: 重构模块",
        "body": "提取公共逻辑"
    })
    assert result is True


@test("CLI 预览展示错误")
def test_cli_display_error():
    from commit_agent.cli import display_preview
    result = display_preview({"error": "出错了"})
    assert result is False


@test("Git 工具 — is_git_repo 检查")
def test_git_is_repo():
    from commit_agent.git_utils import is_git_repo, GitCommandError

    # 当前测试可能不在 git 仓库中，但函数不应崩溃
    result = is_git_repo()
    assert isinstance(result, bool)


@test("Diff 截断 — 长 diff 被截断")
def test_diff_truncation():
    from commit_agent.agent import _truncate_diff, MAX_DIFF_CHARS

    short = "short diff"
    assert _truncate_diff(short) == short

    long_diff = "x" * (MAX_DIFF_CHARS + 100)
    result = _truncate_diff(long_diff)
    assert len(result) < len(long_diff)
    assert "截断" in result


# ---------- 集成测试（可选）----------

@test("集成测试 — 真实 API 调用（跳过）")
def test_integration():
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise AssertionError("未设置 DEEPSEEK_API_KEY，跳过集成测试")

    from commit_agent.llm_client import LLMClient
    client = LLMClient(api_key=api_key)
    response = client.send_with_tools(
        system_prompt="收到任何消息都调用 generate_commit_message 工具返回测试结果。",
        user_message="测试 diff：新增了 login 函数"
    )
    msg = response.choices[0].message
    assert msg.tool_calls is not None, "LLM 应该调用工具，但没有"
    func = msg.tool_calls[0].function
    assert func.name == "generate_commit_message"
    args = json.loads(func.arguments)
    assert "title" in args, f"工具参数缺少 title，收到: {args}"
    print(f"    生成的 title: {args['title']}")


# ---------- 执行 ----------
if __name__ == "__main__":
    print(f"\n{'='*45}")
    print("  git-commit-agent 快速测试")
    print(f"{'='*45}\n")

    # 按顺序执行
    test_prompt_normal()
    test_prompt_empty()
    test_prompt_all_types()
    test_tool_definition()
    test_default_temperature()
    test_agent_normal()
    test_agent_empty_diff()
    test_agent_api_error()
    test_agent_malformed_json()
    test_agent_not_git_repo()
    test_cli_display()
    test_cli_display_error()
    test_git_is_repo()
    test_diff_truncation()

    if "--with-api" in sys.argv:
        print()
        test_integration()

    print(f"\n{'='*45}")
    print(f"  结果: {passed}/{passed + failed} 测试通过")
    if failed > 0:
        print(f"  ❌ {failed} 个测试失败，请检查上面的详细错误")
    else:
        print(f"  ✅ 全部通过！")
    print(f"{'='*45}\n")

    sys.exit(1 if failed > 0 else 0)
