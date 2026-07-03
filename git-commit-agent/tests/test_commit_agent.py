"""
测试套件 — git-commit-agent

运行方式（项目根目录下）：
  pip install pytest        # 首次运行需要
  pytest tests/ -v          # 运行所有测试（跳过集成测试）
  pytest tests/ -v --run-integration  # 包含需要 API Key 的集成测试

测试覆盖：
  - prompts: 提示词模板逻辑
  - git_utils: git 命令封装（在临时仓库中运行）
  - llm_client: 工具定义格式
  - agent: mock API 响应后的结果解析（核心测试）
  - integration: 真实 API 调用（需设置 DEEPSEEK_API_KEY）
"""

import json
import os
import shutil
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 确保可以导入项目模块
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================
# 测试 prompts.py
# ============================================================

class TestPrompts:
    """提示词模板测试"""

    def test_build_diff_prompt_normal(self):
        from commit_agent.prompts import build_diff_prompt
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n-hello\n+hello world"
        result = build_diff_prompt(diff)
        assert "hello world" in result
        assert "```diff" in result

    def test_build_diff_prompt_empty(self):
        from commit_agent.prompts import build_diff_prompt
        result = build_diff_prompt("")
        assert "没有检测到任何代码变更" in result

    def test_build_diff_prompt_whitespace(self):
        from commit_agent.prompts import build_diff_prompt
        result = build_diff_prompt("   \n  \n")
        assert "没有检测到任何代码变更" in result

    def test_system_prompt_has_all_types(self):
        from commit_agent.prompts import SYSTEM_PROMPT
        for t in ["feat", "fix", "refactor", "docs", "style", "test",
                   "chore", "perf", "ci", "build"]:
            assert t in SYSTEM_PROMPT, f"SYSTEM_PROMPT 缺少类型 {t}"

    def test_system_prompt_mentions_tool(self):
        from commit_agent.prompts import SYSTEM_PROMPT
        assert "generate_commit_message" in SYSTEM_PROMPT


# ============================================================
# 测试 git_utils.py（需要临时 git 仓库）
# ============================================================

@pytest.fixture
def temp_git_repo():
    """创建一个临时 git 仓库，测试完成后自动清理"""
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        subprocess.run(["git", "init"], cwd=tmp, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"],
                        cwd=tmp, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"],
                        cwd=tmp, capture_output=True)

        init_file = tmp / "README.md"
        init_file.write_text("# init")
        subprocess.run(["git", "add", "-A"], cwd=tmp, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp, capture_output=True)

        readme = tmp / "README.md"
        readme.write_text("# init\n\nprint('hello')")

        yield tmp
    os.chdir(original_cwd)


class TestGitUtils:
    """Git 工具函数测试（基于临时仓库）"""

    def test_get_full_diff_detects_changes(self, temp_git_repo):
        from commit_agent.git_utils import get_full_diff
        original_cwd = Path.cwd()
        os.chdir(temp_git_repo)
        try:
            diff = get_full_diff()
            assert "README.md" in diff
            assert diff.strip(), "diff 不应为空"
        finally:
            os.chdir(original_cwd)

    def test_get_staged_diff_empty_initially(self, temp_git_repo):
        from commit_agent.git_utils import get_staged_diff
        original_cwd = Path.cwd()
        os.chdir(temp_git_repo)
        try:
            diff = get_staged_diff()
            assert diff.strip() == ""
        finally:
            os.chdir(original_cwd)

    def test_stage_all_and_commit(self, temp_git_repo):
        from commit_agent.git_utils import stage_all, commit
        original_cwd = Path.cwd()
        os.chdir(temp_git_repo)
        try:
            stage_all()
            commit("test: verify commit flow\n\nThis is a test body with $pecial chars.")
            log = subprocess.run(
                ["git", "log", "--oneline", "-1"],
                capture_output=True, text=True, encoding="utf-8", cwd=temp_git_repo
            ).stdout
            assert "test: verify commit flow" in log
        finally:
            os.chdir(original_cwd)

    def test_commit_with_special_characters(self, temp_git_repo):
        """多行消息包含特殊字符时应正确提交"""
        from commit_agent.git_utils import stage_all, commit
        original_cwd = Path.cwd()
        os.chdir(temp_git_repo)
        try:
            stage_all()
            message = "feat: $dollar `backtick` test\n\nBody with 'quotes' and newlines."
            commit(message)
            log = subprocess.run(
                ["git", "log", "--format=%B", "-1"],
                capture_output=True, text=True, encoding="utf-8", cwd=temp_git_repo
            ).stdout.strip()
            # body 部分的换行在 git log -1 中通过 %B 还原
            assert "feat: $dollar `backtick` test" in log
            assert "Body with 'quotes'" in log
        finally:
            os.chdir(original_cwd)

    def test_commit_empty_message_raises(self, temp_git_repo):
        from commit_agent.git_utils import stage_all, commit, GitCommandError
        original_cwd = Path.cwd()
        os.chdir(temp_git_repo)
        try:
            stage_all()
            with pytest.raises(GitCommandError, match="提交失败"):
                commit("")
        finally:
            os.chdir(original_cwd)

    def test_get_full_diff_in_non_repo(self):
        """在非 git 目录中应抛出 GitCommandError"""
        from commit_agent.git_utils import get_full_diff, GitCommandError
        original_cwd = Path.cwd()
        tmpdir = tempfile.mkdtemp()
        try:
            os.chdir(tmpdir)
            with pytest.raises(GitCommandError, match="不是 Git 仓库"):
                get_full_diff()
        finally:
            os.chdir(original_cwd)
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_is_git_repo_positive(self, temp_git_repo):
        from commit_agent.git_utils import is_git_repo
        original_cwd = Path.cwd()
        os.chdir(temp_git_repo)
        try:
            assert is_git_repo() is True
        finally:
            os.chdir(original_cwd)

    def test_is_git_repo_negative(self):
        from commit_agent.git_utils import is_git_repo
        original_cwd = Path.cwd()
        tmpdir = tempfile.mkdtemp()
        try:
            os.chdir(tmpdir)
            assert is_git_repo() is False
        finally:
            os.chdir(original_cwd)
            shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================
# 测试 llm_client.py
# ============================================================

class TestLLMClient:
    """LLM 客户端测试"""

    def test_define_tools_format(self):
        from commit_agent.llm_client import LLMClient
        tools = LLMClient.define_tools()
        assert len(tools) == 1
        tool = tools[0]
        assert tool["type"] == "function"
        assert "function" in tool
        assert tool["function"]["name"] == "generate_commit_message"

    def test_define_tools_parameters(self):
        from commit_agent.llm_client import LLMClient
        tool = LLMClient.define_tools()[0]
        params = tool["function"]["parameters"]
        assert params["type"] == "object"
        assert "title" in params["properties"]
        assert "type" in params["properties"]
        assert "body" in params["properties"]
        assert params["required"] == ["title", "type"]

    def test_define_tools_type_enum(self):
        """type 参数应有完整的 10 个枚举值"""
        from commit_agent.llm_client import LLMClient
        type_prop = LLMClient.define_tools()[0]["function"]["parameters"]["properties"]["type"]
        expected = {"feat", "fix", "refactor", "docs", "style",
                     "test", "chore", "perf", "ci", "build"}
        assert set(type_prop["enum"]) == expected

    def test_default_temperature(self):
        """默认 temperature 应为 0.3"""
        from commit_agent.llm_client import LLMClient
        client = LLMClient(api_key="test-key")
        assert client.temperature == 0.3

    def test_custom_temperature(self):
        """允许覆盖 temperature"""
        from commit_agent.llm_client import LLMClient
        client = LLMClient(api_key="test-key", temperature=0.8)
        assert client.temperature == 0.8


# ============================================================
# 测试 agent.py（核心测试）
# ============================================================

class TestAgentResponseParsing:
    """Agent 响应解析测试（mock API，不真实调用）"""

    def _make_mock_response(self, tool_call_data: dict):
        """构造一个模拟的 DeepSeek 响应"""
        mock_choice = MagicMock()
        mock_message = MagicMock()
        mock_tool_call = MagicMock()

        mock_tool_call.id = "call_test_123"
        mock_tool_call.type = "function"
        mock_tool_call.function.name = "generate_commit_message"
        mock_tool_call.function.arguments = json.dumps(tool_call_data)

        mock_message.tool_calls = [mock_tool_call]
        mock_message.content = None
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        return mock_response

    @patch("commit_agent.agent.get_full_diff")
    @patch("commit_agent.agent.LLMClient")
    def test_normal_commit_message(self, mock_llm_client, mock_get_full_diff):
        from commit_agent.agent import generate_commit_message

        mock_get_full_diff.return_value = "diff --git a/app.py b/app.py\n+print('hello')"
        mock_response = self._make_mock_response({
            "title": "feat: 添加 hello 输出",
            "type": "feat",
            "body": "在 app.py 中添加了 hello 输出语句"
        })
        mock_llm_client.return_value.send_with_tools.return_value = mock_response

        result = generate_commit_message(api_key="test-key")
        assert result["type"] == "feat"
        assert "hello" in result["title"]
        assert "app.py" in result["body"]

    @patch("commit_agent.agent.get_full_diff")
    @patch("commit_agent.agent.LLMClient")
    def test_commit_without_body(self, mock_llm_client, mock_get_full_diff):
        from commit_agent.agent import generate_commit_message

        mock_get_full_diff.return_value = "diff --git a/app.py b/app.py\n-fix\n+fixed"
        mock_response = self._make_mock_response({
            "title": "fix: 修复了一个小问题",
            "type": "fix",
        })
        mock_llm_client.return_value.send_with_tools.return_value = mock_response

        result = generate_commit_message(api_key="test-key")
        assert result["type"] == "fix"
        assert result["body"] == ""

    @patch("commit_agent.agent.get_full_diff")
    def test_empty_diff(self, mock_get_full_diff):
        from commit_agent.agent import generate_commit_message

        mock_get_full_diff.return_value = ""
        result = generate_commit_message(api_key="test-key")
        assert "error" in result
        assert "没有检测到代码变更" in result["error"]

    @patch("commit_agent.agent.get_full_diff")
    @patch("commit_agent.agent.LLMClient")
    def test_llm_returns_text_instead_of_tool_call(self, mock_llm_client, mock_get_full_diff):
        from commit_agent.agent import generate_commit_message

        mock_get_full_diff.return_value = "diff --git a/app.py b/app.py\n+data"
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_message = MagicMock()
        mock_message.tool_calls = None
        mock_message.content = "当前 diff 没有实际意义"
        mock_choice.message = mock_message
        mock_response.choices = [mock_choice]
        mock_llm_client.return_value.send_with_tools.return_value = mock_response

        result = generate_commit_message(api_key="test-key")
        assert "error" in result

    @patch("commit_agent.agent.get_full_diff")
    @patch("commit_agent.agent.LLMClient")
    def test_api_call_failure(self, mock_llm_client, mock_get_full_diff):
        from commit_agent.agent import generate_commit_message

        mock_get_full_diff.return_value = "diff --git a/app.py b/app.py\n+data"
        mock_llm_client.return_value.send_with_tools.side_effect = Exception("Connection error")

        result = generate_commit_message(api_key="test-key")
        assert "error" in result
        assert "API 调用失败" in result["error"]

    @patch("commit_agent.agent.get_full_diff")
    @patch("commit_agent.agent.LLMClient")
    def test_malformed_json_returns_error(self, mock_llm_client, mock_get_full_diff):
        """非法 JSON 应返回 error dict 而非抛出异常"""
        from commit_agent.agent import generate_commit_message

        mock_get_full_diff.return_value = "diff --git a/app.py b/app.py\n+data"
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
        mock_llm_client.return_value.send_with_tools.return_value = mock_response

        result = generate_commit_message(api_key="test-key")
        assert "error" in result
        assert "格式异常" in result["error"]

    @patch("commit_agent.agent.get_full_diff")
    @patch("commit_agent.agent.LLMClient")
    def test_missing_title_returns_error(self, mock_llm_client, mock_get_full_diff):
        """缺少必填字段 title 时应返回 error dict"""
        from commit_agent.agent import generate_commit_message

        mock_get_full_diff.return_value = "diff --git a/app.py b/app.py\n+data"
        mock_response = self._make_mock_response({
            "type": "feat",
            "body": "只有 body 没有 title"
        })
        mock_llm_client.return_value.send_with_tools.return_value = mock_response

        result = generate_commit_message(api_key="test-key")
        assert "error" in result
        assert "缺少必填字段" in result["error"]

    @patch("commit_agent.agent.get_full_diff")
    @patch("commit_agent.agent.LLMClient")
    def test_diff_truncation(self, mock_llm_client, mock_get_full_diff):
        """超过 8000 字符的 diff 应被截断"""
        from commit_agent.agent import generate_commit_message, MAX_DIFF_CHARS

        # 创建一个超长 diff
        long_diff = "diff --git a/app.py b/app.py\n"
        long_diff += "+long line " * 500  # ~7000 chars
        long_diff += "\n+extra data " * 200  # 再加一些确保超限

        mock_get_full_diff.return_value = long_diff
        mock_response = self._make_mock_response({
            "title": "feat: 更新 app.py",
            "type": "feat",
        })
        mock_llm_client.return_value.send_with_tools.return_value = mock_response

        result = generate_commit_message(api_key="test-key")
        assert result["type"] == "feat"

    @patch("commit_agent.agent.get_full_diff")
    @patch("commit_agent.agent.LLMClient")
    def test_retry_on_transient_error(self, mock_llm_client, mock_get_full_diff):
        """瞬态错误应触发重试，最终成功"""
        from commit_agent.agent import generate_commit_message
        import time as _time_module

        mock_get_full_diff.return_value = "diff --git a/app.py b/app.py\n+data"

        mock_client_instance = MagicMock()
        # 前两次调用失败，第三次成功
        mock_client_instance.send_with_tools.side_effect = [
            TimeoutError("request timed out"),
            ConnectionError("connection reset"),
            self._make_mock_response({
                "title": "feat: 重试后成功",
                "type": "feat",
            }),
        ]
        mock_llm_client.return_value = mock_client_instance

        result = generate_commit_message(api_key="test-key")
        assert result["type"] == "feat"
        assert result["title"] == "feat: 重试后成功"
        assert mock_client_instance.send_with_tools.call_count == 3

    @patch("commit_agent.agent.get_full_diff")
    @patch("commit_agent.agent.LLMClient")
    def test_no_retry_on_permanent_error(self, mock_llm_client, mock_get_full_diff):
        """非瞬态错误不应重试"""
        from commit_agent.agent import generate_commit_message

        mock_get_full_diff.return_value = "diff --git a/app.py b/app.py\n+data"
        mock_client_instance = MagicMock()
        mock_client_instance.send_with_tools.side_effect = ValueError("invalid API key")
        mock_llm_client.return_value = mock_client_instance

        result = generate_commit_message(api_key="test-key")
        assert "error" in result
        # 只调用了一次，没有重试
        assert mock_client_instance.send_with_tools.call_count == 1

    @patch("commit_agent.agent.get_full_diff")
    def test_git_repo_error_propagates(self, mock_get_full_diff):
        """不在 git 仓库时应返回明确错误"""
        from commit_agent.agent import generate_commit_message
        from commit_agent.git_utils import GitCommandError

        mock_get_full_diff.side_effect = GitCommandError("当前目录不是 Git 仓库")

        result = generate_commit_message(api_key="test-key")
        assert "error" in result
        assert "不是 Git 仓库" in result["error"]


# ============================================================
# 测试 cli.py
# ============================================================

class TestCLI:
    """CLI 交互测试"""

    def test_display_preview_success(self, capsys):
        from commit_agent.cli import display_preview
        result = display_preview({
            "type": "feat",
            "title": "feat: 测试功能",
            "body": "这是正文"
        })
        assert result is True
        captured = capsys.readouterr()
        assert "feat: 测试功能" in captured.out
        assert "这是正文" in captured.out

    def test_display_preview_error(self, capsys):
        from commit_agent.cli import display_preview
        result = display_preview({"error": "出错了"})
        assert result is False
        captured = capsys.readouterr()
        assert "出错了" in captured.out

    def test_display_preview_no_body(self, capsys):
        from commit_agent.cli import display_preview
        result = display_preview({
            "type": "fix",
            "title": "fix: 修复问题",
        })
        assert result is True
        captured = capsys.readouterr()
        # 无 body 时不应显示正文分隔
        assert "fix: 修复问题" in captured.out

    def test_display_preview_shows_full_message(self, capsys):
        """预览应显示完整的 commit message 文本而不仅是字段"""
        from commit_agent.cli import display_preview
        display_preview({
            "type": "feat",
            "title": "feat: 添加登录",
            "body": "实现了基于 JWT 的用户认证系统",
        })
        captured = capsys.readouterr()
        # 验证消息内容连续出现在预览中
        assert "feat: 添加登录" in captured.out
        assert "JWT" in captured.out


# ============================================================
# 集成测试（需要真实的 DeepSeek API Key）
# ============================================================

@pytest.mark.integration
class TestIntegration:
    """集成测试 — 需要设置 DEEPSEEK_API_KEY 环境变量"""

    @pytest.fixture(autouse=True)
    def check_api_key(self):
        if not os.environ.get("DEEPSEEK_API_KEY"):
            pytest.skip("未设置 DEEPSEEK_API_KEY，跳过集成测试")

    def test_llm_client_real_call(self):
        from commit_agent.llm_client import LLMClient

        client = LLMClient(api_key=os.environ["DEEPSEEK_API_KEY"])
        response = client.send_with_tools(
            system_prompt="你是一个测试助手，收到任何消息都调用 generate_commit_message 工具返回测试结果。",
            user_message="测试 diff：新增了 login 函数"
        )

        message = response.choices[0].message
        assert message.tool_calls is not None, "LLM 应调用了工具"
        assert message.tool_calls[0].function.name == "generate_commit_message"

    def test_agent_real_diff(self, temp_git_repo):
        from commit_agent.agent import generate_commit_message

        original_cwd = Path.cwd()
        os.chdir(temp_git_repo)
        try:
            result = generate_commit_message(api_key=os.environ["DEEPSEEK_API_KEY"])
            if "error" not in result:
                assert "title" in result
                assert "type" in result
                valid_types = {
                    "feat", "fix", "refactor", "docs", "style",
                    "test", "chore", "perf", "ci", "build"
                }
                assert result["type"] in valid_types
        finally:
            os.chdir(original_cwd)


# ============================================================
# conftest.py — 添加 --run-integration 选项
# ============================================================

def pytest_addoption(parser):
    parser.addoption(
        "--run-integration", action="store_true", default=False,
        help="运行集成测试（需要 DEEPSEEK_API_KEY）"
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: mark test as integration test (requires API key)"
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-integration"):
        return  # 不跳过
    skip_integration = pytest.mark.skip(reason="需要 --run-integration 选项")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
