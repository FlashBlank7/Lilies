"""DeepSeek API 客户端封装（OpenAI 兼容格式）"""

from openai import OpenAI


class LLMClient:
    """封装 DeepSeek API，管理 API 调用和工具定义"""

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
        temperature: float = 0.3,
    ):
        self.client = OpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
        )
        self.model = model
        self.temperature = temperature

    @staticmethod
    def define_tools() -> list:
        """工具定义（OpenAI Function Calling 格式）

        DeepSeek 完全兼容 OpenAI 的 tool 定义格式。
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": "generate_commit_message",
                    "description": (
                        "根据 git diff 生成符合 Conventional Commits 规范的 commit message。"
                        "分析变更的内容、类型和影响范围，返回规范的提交信息。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": (
                                    "commit 标题，格式为 <type>: <中文描述>，不超过 72 字符。"
                                    "例如：feat: 添加用户登录功能"
                                ),
                            },
                            "type": {
                                "type": "string",
                                "enum": [
                                    "feat",
                                    "fix",
                                    "refactor",
                                    "docs",
                                    "style",
                                    "test",
                                    "chore",
                                    "perf",
                                    "ci",
                                    "build",
                                ],
                                "description": "变更类型（Conventional Commits 完整类型集）",
                            },
                            "body": {
                                "type": "string",
                                "description": (
                                    "commit 正文（可选），使用中文说明变更原因和详细内容，"
                                    "每行不超过 72 字符"
                                ),
                            },
                        },
                        "required": ["title", "type"],
                    },
                },
            }
        ]

    def send_with_tools(
        self,
        system_prompt: str,
        user_message: str,
        timeout: float = 120.0,
    ):
        """发送消息（带工具定义），返回 DeepSeek 原始响应。

        返回 OpenAI 风格的 ChatCompletion 对象。
        tool_calls 在 response.choices[0].message.tool_calls 中。
        """
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            tools=self.define_tools(),
            max_tokens=1024,
            temperature=self.temperature,
            timeout=timeout,
        )
        return response
