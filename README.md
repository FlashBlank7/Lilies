# 智能体/工作流生成平台

通过项目内对话，将企业数据与目标转化为可编辑、可运行、可继续改进的工作流。当前项目入口由平台自己的智能体循环运行，连接模型 API 提供推理，并可调整思考模式。之前由本机 Codex 完成的业务试用保留为历史，不能据此认定平台自身智能体目标达成。

## 当前能用什么

- **打开项目**：导入需求包，沟通目标；导入不会自动执行。
- **分析与建模**：数据分析、特征配置、标准库训练、Optuna 调参和模型比较；每次试验保留训练笔记、实际指标和产物。
- **交付与继续**：下载模型、运行预测工作流、反馈修改，以及停止后继续。
- **编辑业务流程**：主流程与成员工作流共用现有画布和运行器，共享项目记录。

通用链条已有真实运行，工业模型效果仍需逐项目验证。客户试验记录不随公开源码发布，平台可运行不等于模型精度达标。

## 本机原型启动

v0.6 的独立训练、随时生成草稿和模型绑定入口见 [原型试用说明](docs/V06_TRYOUT.md)。

需要 Python 3.12+、Node.js 20+、Docker。项目使用模型 API，计算由 Docker 容器执行。配置、思考选项与验证范围见[项目模型连接](docs/project-model-connections.md)。

以下是当前开发启动方式；完整备份恢复已实现并完成本机隔离验证，干净环境安装和生产启动仍待验证。

1. 首次创建配置；已有配置无需覆盖：

   ```sh
   cp .env.example .env
   chmod 600 .env
   ```

   将 `API_TOKEN` 换为自己的随机令牌。保持 `MODEL_EGRESS_ENABLED=false`，在项目模型设置中明确配置 API 连接；不会自动启用全局模型或调用上传材料中的密钥。

2. 安装平台依赖：

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install -e '.[dev]'
   npm --prefix platform/frontend ci
   ```

3. 准备两个计算镜像：

   ```sh
   docker build --build-arg SANDBOX_UID=$(id -u) --build-arg SANDBOX_GID=$(id -g) -f Dockerfile.sandbox -t agent-platform-sandbox:latest .
   docker build -f Dockerfile.modeling -t lilies-modeling:20260914 .
   ```

   本机运行时，将 `.env` 中 `SANDBOX_UID`、`SANDBOX_GID` 设置为构建所用的用户与组编号。已有研究绑定固定镜像，升级时保留这些镜像。建模环境及已知安装边界见[建模说明](docs/modeling.md)。

4. 检查配置并启动：

   ```sh
   ./scripts/dev_platform.sh --check-env
   ./scripts/dev_platform.sh
   ```

   打开 **http://127.0.0.1:3000/projects**。API 默认 http://127.0.0.1:8001。`--check-env` 仅显示配置和程序发现情况，实际登录、镜像及计算需要在项目中验证。

旧 `compose.yaml` 保留兼容用途，但尚未包含建模镜像和宿主 Codex 的完整部署路径，不能将其作为当前原型的一键交付方案。不要为了启动旧入口而打开平台模型出口。

## 本轮开发重点

优先交付能独立试用的原型：精简旧入口和配置、减少统筹往返、准备完整数据备份恢复，并在干净环境完成发布候选试用。部署位置与模型付款、报销方式仍待确认；当前不据此扩建计费或多租户系统。

- [每周开发计划](docs/DEVELOPMENT_PLAN.md)：单人开发，暂按每周3个投入日，推进原型落地与工业项目验证。
- [原型试用说明](docs/PROTOTYPE_QUICKSTART.md)：从上传数据到结果、反馈与预测。
- [产品方向](docs/PRODUCT_NORTH_STAR.md)、[业务结构](docs/BUSINESS_LOGIC.md)。
- [数据与建模](docs/modeling.md)、逐模型笔记及实际试用（本地试验记录）。
- [项目协作](docs/project-cooperation.md)、[统一对话与接续](docs/project-conversation.md)。

当前业务文件分布在 `data/` 与 `workspaces/`。`scripts/backup.sh` 已覆盖两者及配置，停止平台后备份；恢复只写入新目录。操作和边界见[完整备份与恢复](docs/backup-restore.md)。只备份 SQLite 不能恢复整个项目。

## 开发验证

```sh
MODEL_EGRESS_ENABLED=false .venv/bin/python -m pytest tests -q
npm --prefix platform/frontend test -- --run
npm --prefix platform/frontend run build
```

旧单应用入口位于 `/applications`，现有工作流及接口保留兼容。历史研究和版本记录见原 README 归档（本地试验记录），不作为当前开发顺序或产品完成声明。
