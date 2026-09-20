# 本机 Agent 历史说明 接入

2026-09-17：下文描述旧的外部 Agent 接入，仅供历史和独立应用兼容参考。项目主路径已改为模型 API + 平台自身智能体循环，不能再按下文连接本机 Codex 来验证平台目标。当前用法见[项目模型连接](project-model-connections.md)。

平台管理需求包、需求沟通、当前草稿和运行结果。Codex 负责分析、提问、查询积木、增量搭建和根据实际运行结果修改。接入实现不包含工业项目算法或历史答案。

## 使用

打开项目会话页，选择「本机 Codex」，可手动指定可执行文件路径和模型。留空路径从平台后端进程的 PATH 检测；留空模型由 Codex 默认模型决定。需要 Codex CLI 0.153.0 或更新版本；本次真实验证使用 0.153.4。

点击「使用本机 Codex」仅保存选择。点击「分析资料」才启动本机 `codex app-server --stdio`，沿用该 CLI 已有登录。未登录或版本不兼容时直接显示错误，不安装、不自动登录、不替换为 DeepSeek。

Agent 先读取资料并提出理解/问题；用户在应用中回复。Agent 提交需求文档后，用户确认后，在项目对话中说“开始搭建”或继续原事项；独立应用仍保留原有入口。运行时可发送补充消息，也可停止。再次继续会恢复平台记录的本项目 Codex thread。平台重启不自动续跑，显示中断状态，等待用户继续。

编辑器升级导致已保存的 Codex 程序路径失效时，下一次继续会从平台进程的 PATH 重新发现 Codex 并验证版本。只更新可执行程序及版本信息，保留原会话、thread、需求确认和历史记录；其他文件缺失仍按实际错误报告，不通过重建会话掩盖。

使用完整备份恢复到新目录时，离线恢复工具会迁移复制后 CLI 会话索引中的运行路径，不改会话内容或 thread 编号。重连优先使用本项目恢复目录内的保存文件，登录链接指向当前电脑已有登录。已验证真实 CLI 重连回读的路径属于恢复目录，未启动新的模型回合；详见[恢复说明](backup-restore.md)。

建设中的连续进展不受固定 15 分钟总时长限制。连接连续 15 分钟没有响应且没有平台工具正在执行时，才中断并显示原因和继续提示；平台工具保留各自的运行时限。用户主动停止仍会取消会话及其未完成运行。一个模型回合完成只表示该回合结束，未完成的项目需保留剩余事项、阻塞原因和恢复后的第一步。

Codex 自带文件系统保持只读且不提供原生 Shell；这不限制已授权的项目工具。搭建阶段使用 `project_file` 写入 `solution/`、`results/`，使用 `workflow_draft` 编辑画布，由平台检查范围与权限。缺少标准依赖时依据用户已有授权补齐运行环境，不能默默改写简化算法来迁就缺包。

工作流仍在现有画布中编辑、由现有运行时执行。Codex 每轮读取当前草稿，修改使用原有 revision/idempotency 接口。画布中的旧自然语言编辑入口会提示转到 Codex 项目会话，手工编辑照常。

## 进程和输入范围

- 每个项目拥有独立 Codex home 与新 thread，不附加到用户已有会话，不加载个人插件、记忆和项目外指令。只有本机 CLI 登录文件被链接用于认证，平台不把凭证送入模型上下文。
- `thread/start` 和 `turn/start` 均指定空 environments，关闭 Codex 原生文件/Shell 环境访问。业务操作通过五个宿主工具完成；不支持的服务器请求直接拒绝。
- Agent 只能读 `requirement-package/`、本次 `solution/`、`results/` 及确认的需求文档。项目工具拒绝绝对路径、路径穿越和符号链接；写入仅限 solution/results。
- 选用 Codex 时，旧的未确认需求讨论保存在平台 data 目录，新的 Agent 不读取它。已由用户确认的需求仍作为项目输入保留。
- 通过 Agent 启动的运行限定当前项目，不允许调用项目外应用、现有 Program 配置或外网连接器；工作流模型调用需在本项目模型设置中显式启用。代码工具在 Docker 沙箱中运行，需求包与需求记录挂载为只读。现有通用 sandbox 镜像提供运行依赖。
- 本地管理和本地 Agent 进程不等于离线模型；Codex 模型调用遵循它自己的登录。`MODEL_EGRESS_ENABLED=false` 继续关闭平台内置模型出口，本地会话不会打开该开关。

## HTTP v1 接口

这些接口沿用平台认证。Codex 适配层不接收平台管理令牌；它使用绑定到一个 application_id 的动态工具。未来适配层可复用相同工具合同，自研 Agent 保持独立进程，通过版本化 HTTP 接入。

| 接口 | 用途 |
| --- | --- |
| `GET /api/v1/local-agents` | 发现 Codex、Claude Code、Kimi 程序与版本 |
| `GET/PUT /api/v1/applications/{id}/agent-session` | 读取状态，或选择 `{provider, executable?, model?}` |
| `POST .../agent-session/messages` | `{message, intent: "discuss"或"build"}`；202 后轮询会话；运行中同阶段消息走 steer |
| `POST .../agent-session/stop` | 停止 Agent 与它仍在运行的项目工作流 |
| `GET/POST .../agent-tools` | 查看工具 JSON Schema，或调用 `{name, arguments}` |
| `POST .../requirements/confirm` | `{revision}`；沿用需求文档确认接口 |

五个工具为 `project_file`、`block_catalog`、`workflow_draft`、`workflow_run`、`requirements_submit`。工具参数以 GET 返回的 Schema 为准，`contract_version` 为 1。会话保存在 `data/local-agents/{application_id}/`，工作流仍由平台原有存储管理。

## 验证和当前范围

自动测试覆盖无调用的选择操作、需求沟通与确认、真实平台运行、人工编辑后继续、停止与 thread 恢复、重启不自动运行、错误不回退、项目路径限制和 stdio 工具返回。

2026-09-10 使用真实本机 Codex 0.153.4，在独立的文本回显需求包上完成：读取原文并提问 → 收到答复后生成文档 → 用户确认 → 查询积木并增量生成 4 个节点 → 11 次平台运行（含验收运行），7 项保存的验收测试通过。关闭并重建应用后，真实 Codex 成功恢复原 thread，读取原草稿的 4 个节点和 7 项测试，未修改草稿。另用真实 Docker 容器验证需求输入可读、改写报只读错误、结果目录可写。这些是平台接入验证，不是工业项目业务验收。

Claude Code、Python Kimi CLI 与 API 模型现已复用项目工具接入，工作流模型可在项目页面启用；真实账号验证边界见模型连接说明。自研 Agent 驱动、桌面安装包及原生跨厂商会话迁移仍未实现；切换模型保留平台项目历史，通过新模型线程接续。没有引入共享数据库或直接导入自研 Agent 源码。

Codex 协议参考：[官方 App Server 文档](https://learn.chatgpt.com/docs/app-server)。该接口目前具有实验性字段；不兼容时显示错误，不能降低项目输入限制来强行启动。

项目模式沿用完整的增量编辑合同：`update_node.data` 使用 `node_id`、`changes` 和可选 `merge_config`。`block_catalog(tool_name="workflow_draft")` 可查询工具全文、Schema 和可执行参数示例，也支持查询其他项目 Agent 工具。错误的更新字段会直接提示正确格式，不再误报配置相同。连续失败暂停仍保留需求确认；修复后通过原项目新消息继续，不能要求重新确认已经确认的需求。
