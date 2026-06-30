# git-commit-agent

🤖 AI-powered Git commit message generator — 在 Git 仓库中自动生成符合 [Conventional Commits](https://www.conventionalcommits.org/) 规范的中文 commit message。

## 功能

- 读取 `git diff`（staged + unstaged），通过 DeepSeek API 分析代码变更
- 生成包含 **10 种类型**（feat/fix/refactor/docs/style/test/chore/perf/ci/build）的结构化中文 commit message
- 展示完整预览，用户确认后执行提交
- 支持 `--stage` 自动暂存、`--yes` 跳过确认（脚本化场景）

## 前置条件

- Python >= 3.10
- DeepSeek API Key（注册地址：https://platform.deepseek.com）

## 安装

### 方式一：从 .whl 安装（推荐）

```bash
pip install git_commit_agent-0.2.0-py3-none-any.whl
```

### 方式二：从源码安装

```bash
cd git-commit-agent
pip install -e .
```

### 方式三：开发者安装（含测试依赖）

```bash
cd git-commit-agent
pip install -e ".[dev]"
```

## 配置

设置环境变量：

```bash
# Mac / Linux
export DEEPSEEK_API_KEY=sk-your-key-here

# Windows PowerShell
$env:DEEPSEEK_API_KEY='sk-your-key-here'
```

## 使用

```bash
# 基本用法：在 Git 仓库中运行，生成 commit message 并确认提交
commit-agent

# 自动暂存所有变更后再生成 commit
commit-agent --stage

# 跳过确认提示，直接提交（适用于 CI/脚本）
commit-agent --stage --yes

# 指定模型
commit-agent --model deepseek-chat
```

## 支持的 Commit 类型

| 类型 | 说明 |
|------|------|
| feat | 新功能 |
| fix | 修复 bug |
| refactor | 代码重构 |
| docs | 文档变更 |
| style | 代码格式调整 |
| test | 添加或修改测试 |
| chore | 构建/工具/依赖变更 |
| perf | 性能优化 |
| ci | CI/CD 配置变更 |
| build | 构建系统或外部依赖变更 |

## 构建 .whl 分发包

```bash
pip install build
python -m build --wheel
# 产出: dist/git_commit_agent-0.2.0-py3-none-any.whl
```

## 开发测试

```bash
# 运行单元测试（不需要 API Key）
pytest tests/ -v

# 运行快速测试（不需要 API Key）
python quick_test.py

# 运行集成测试（需要 API Key）
pytest tests/ -v --run-integration
```
