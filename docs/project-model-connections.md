# 项目模型连接

2026-09-17：项目由平台自己的 Lilies 会话循环执行。外部模型通过 API 提供推理和工具调用请求；平台执行工具、保存消息与结果、管理停止和恢复。项目入口仅接受 `provider=api`，不再把 Codex / Claude Code / Kimi CLI 会话当成项目智能体。旧会话和产物保留，更换为 API 连接后才能继续。

## 使用

1. 打开项目 → 设置 → 模型设置，选择 OpenAI 兼容或 Anthropic Messages 协议，填写地址、准确模型名和 API Key。
2. 选择思考模式，保存连接。保存不调用模型；发送消息或运行模型节点才消耗服务商额度。已有其他项目连接时，可展开“沿用已有项目连接”，选择来源项目和用途，无需再次输入密钥。
3. 工作流本身需要推理时，勾选“工作流可调用此模型”。该项目及其成员的普通模型节点使用主连接；代码容器仍无外网，程序不能读取密钥。
4. 项目 → 设置 → 视觉模型设置，可独立配置支持图片的 API 模型，也可复制已有主模型连接后修改模型名。此操作不改变项目主模型或对话。
5. 项目 → 设置 → 搭建能力，明确是否允许智能体积木，默认禁止。禁止时，`claude_agent`、`subagent_spawn` 完整智能体积木不出现在当前项目的手册、目录、架构蓝图和画布积木库中；软积木的启动子智能体策略也禁止使用，包含这些积木的模板不会出现在项目的模板库中；仍可使用 `llm`、`model_turn`、代码、工具、循环构建专用智能体。允许时可配置现有完整智能体积木，模型、工具、网络的原有范围仍然生效。

能力配置接口为 `PUT /api/v1/projects/{id}/capabilities`，字段 `agent_modules_enabled` 为布尔值，仅由用户设置，项目搭建工具不能自行修改。成员继承项目配置；后端在草稿保存、直接运行、嵌套流程和恢复后执行节点时检查。切换为禁止后，旧草稿保留供编辑，但其中的智能体节点不能执行。目录/手册接口通过 `application_id` 选择当前项目；无上下文的公共目录默认不列出完整智能体积木。

原始 API 模型由 `ConnectedModel` 接入；历史本机 CLI 会话通过独立的 `ConnectedAgent` 适配，保持原有兼容入口。CLI 会话不会因开启“工作流可调用此模型”而成为原始 LLM 节点的模型。企业场景使用本地模型服务或客户已配置的可信模型 API，不会自动改用外部智能体。

手工搭建不需要先发起 Lilies 对话：项目 → 工作流创建空白流程，在画布配置并保存节点 → 运行此工作流 → 填写输入或选择项目文件 → 启动工作流。运行页面可以停止、查看实际输入输出与步骤、下载结果文件。

图文节点沿用 `llm`，设置 `model_role: "vision"`，在 `images` 配置项目文件引用列表，例如 `[{"file_path":"results/pages/4.png"}]`；列表也可使用上游 `$ref`。目前支持 PNG、JPEG，每次最多 8 张、合计 20 MB。图片从本次运行工作目录读取，不能引用宿主机绝对路径。未配置或未启用视觉连接时明确失败，不会改用主模型。纯文本节点的默认 `model_role` 为 `main`，原配置保持兼容。

OpenAI 兼容适配器在地址后请求 `/chat/completions`；Anthropic 适配器请求 `/v1/messages`，地址已以 `/v1` 结尾时只追加 `/messages`。本地提供同等 HTTP 协议的模型服务也可以使用；CLI 登录会话不属于这一接入方式。服务商和具体模型必须支持所选参数；不确定时选择“模型默认”。失败不会自动改用另一模型或账号。

| 协议 | 适配器发送的思考参数 | 会话与工具 |
| --- | --- | --- |
| OpenAI 兼容 API | 默认不传；其余传 `reasoning_effort`，关闭为 `none` | 原生 function tool calls，平台执行并回传结果 |
| Anthropic Messages API | 默认不传；关闭为 disabled；开启按模型发送 enabled 或 adaptive，强度选项另传 `output_config.effort` | 原生 tool_use / tool_result，平台保存并续接 |

这些是适配器行为，不能保证每个兼容服务支持所有参数。

## 保存与恢复

接口：`PUT /api/v1/projects/{id}/agent-session`。字段：`provider=api`、`model`、`thinking`、`protocol`、`base_url`、`api_key`、`runtime_enabled`。同一项目从 applications 兼容接口访问也不能改用外部 Agent。

API Key 保存在后端 `data_dir/model-connections/{project_id}.json`，文件权限 0600，不写入工作区、流程草稿、对话或响应。响应仅返回 `has_api_key`。相同地址和协议下留空保留密钥；更换地址或协议必须重新填写。备份数据目录仍需按含凭据的数据保管。

独立视觉连接使用 `GET/PUT /api/v1/projects/{id}/vision-model`，保存为 `{project_id}.vision.json`。页面复制连接使用 `POST /api/v1/projects/{id}/model-connection/copy`，参数为 `source_project_id`、目标 `role` 和可选 `source_role`；密钥仅在后端复制，两个连接随后各自保存。

`ModelSession` 负责模型调用 → 工具执行 → 返回结果 → 下一次推理的循环。`conversation.json` 保存消息与工具结果。调整思考模式或平台工具说明后保留会话；改变 API 地址、协议或模型时建立新会话，并携带原项目近期消息接续。运行中不能更换连接。停止时把中断的工具记录为中断，继续时要求先核对结果，不自动重放写入。

`MODEL_EGRESS_ENABLED=false` 保持全局默认。明确保存的项目 API 连接只供当前项目使用；工作流另需启用调用开关。旧的 CLI 连接即使保存了工作流调用开关，也不会获得模型节点执行权。不会读取上传资料中的密钥，也不会自动使用全局供应商。

## 已验证与未完成

离线测试使用模拟 HTTP 推理回复，验证真实平台会话、工具、草稿与工作流运行器：空白流程搭建、运行失败、读取错误、修改节点、再次运行成功，以及会话续接、密钥隔离和取消。模拟推理不证明真实模型能够独立完成企业任务。

## 文档运行环境

`Dockerfile.documents` 在原数值计算镜像上加入 pypdf、python-docx、reportlab、Poppler（含中文映射 poppler-data）、LibreOffice Writer、Tesseract（含简体中文）和 Noto CJK 字体。数值建模使用的独立镜像不变。

```sh
docker build -f Dockerfile.documents -t agent-platform-sandbox:documents-20260916.1 .
# 网络需要时可传 --build-arg DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn
```

在 `.env` 设置 `SANDBOX_IMAGE=agent-platform-sandbox:documents-20260916.1` 后重启后端。本机已在任务空闲后加载该镜像，原项目会话保留。保留原镜像便于已有环境回退；在其他环境启用前需实际验证文档读取和报告生成。工具安装不代表扫描件识别质量或业务评审正确性已经验证。

已有项目可在“项目资料 → 添加资料”追加设计文档、源码 ZIP 等文件，每个文件最多 256 MB。同名文件独立保存，不替换原输入；上传本身不会启动模型。上传后在运行工作流页面选择本次资料，也可在对话中说明用途。

原始资料已在其他项目时，可通过“项目资料 → 从已有项目选择资料”选择来源项目和文件，添加本项目的独立副本。此入口仅列出原始资料，保持文件内容与文件名；同名副本保存在不同目录，添加后可以在运行输入中选择。接口为 `POST /api/v1/projects/{id}/materials/copy`，输入 `source_project_id`、`source_path`，返回与上传相同的 `name/path/size/sha256`。部分添加失败后，页面保留未成功文件供重试。