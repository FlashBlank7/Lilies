# 企业资料导入与需求沟通

首页选择「导入需求包」，上传 ZIP，再点击「导入并打开任务」。输入只需企业原始材料、数据和简短诉求，不要求用户预先提供整理好的需求文档。应用创建空白任务，将资料放入 `requirement-package/`，进入会话页。导入本身不调用模型、不运行包内代码。

会话流程：选择本项目搭建者 → 点击「分析资料」→ Agent 阅读资料、说明理解并提问 → 用户回复或直接纠正 → Agent 整理需求文档 → 用户点击「理解正确，确认需求文档」→ 再选择「开始搭建」。文档可供核对不表示用户已确认。确认操作只保存文档与任务需求，不自动构建或发布。第一期支持本机 Codex 与原有平台内置 Agent，见 [本机 Agent 接入](local-agents.md)。

ZIP 根目录需要以下说明文件，也支持多一层外部文件夹：

```json
{
  "format": "lilies-requirement-package",
  "version": 1,
  "name": "企业项目名称",
  "description": "项目简介（可选）",
  "requirement_file": "README.md"
}
```

`requirement_file` 是包内 UTF-8 Markdown/TXT 起始诉求的相对路径，不超过 24000 字符或 100 KB。例如「这是我们的项目材料，请先了解并与我们沟通需求」。它不是完成后的需求文档。客户现有说明、规则、表格、CSV/JSON 数据等同包携带，保留来源差异，由平台分析后与人核对。文件原样保存在本地应用工作区。

接口：`POST /api/v1/requirement-packages/import`，使用平台认证，multipart 字段名为 `file`。成功返回 201：`{application, package: {name, requirement_file, file_count, size_bytes, files}}`。格式错误返回 422，不创建任务。

限制：ZIP 最大 256 MB；解压后最大 1 GB、2000 个文件。拒绝路径穿越、符号链接、加密文件、重复名称及文件/目录冲突。忽略 `.DS_Store` 和 ZIP 根部的 `__MACOSX/` 元数据。

选择平台内置 Agent 时，分析复用现有需求补全模型。平台读取文档内容、JSON数组总条数和首条样例，并对CSV/TSV扫描最多50万行，提供字段空值计数、值示例与前5行明细。文档单文件最多24000字符；整体内容最多160000字符。所有截断、未解析文件和扫描是否完整均明确标记给模型，不能据摘要声称全量业务校验。选择 Codex 时，由它主动通过项目工具读取文件、扫描 CSV、查询积木与编辑草稿，不经过内置模型。其他文件格式可由搭建后的工作流处理；能力不足时应报告具体缺口。

沟通记录持久化到 `requirements/conversation.json`；确认的文档为 `requirements/requirements.md`，同时写入应用需求供后续Builder使用。重新打开页面可以继续沟通；模型未返回文档时不会用企业原文代填。

沟通接口（平台认证）：

- `GET /api/v1/applications/{id}/requirements`：读取沟通状态。
- `POST /api/v1/applications/{id}/requirements/messages`：`{revision, message}`；首轮message可空。
- `POST /api/v1/applications/{id}/requirements/confirm`：`{revision}`，确认正在查看的文档版本。

包的说明与附件属于用户输入资料，平台不会将其作为系统配置执行。平台内置模型出口遵守 `MODEL_EGRESS_ENABLED`。本机 Codex 使用自己的登录，通过用户明确发送的项目会话指令启动；选择程序和导入文件本身不会启动 Agent。连接失败不会回退到平台内置模型。脚本化测试回复不能写进企业任务。
