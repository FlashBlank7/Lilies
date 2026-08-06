# 小微企业收据对账、受控入账与审计工件

我们使用 Actual Budget 管理一家小型服务公司的经营账户。银行流水已导入
Actual，财务人员另外收到一批经过 OCR 和人工初校的收据事实。请搭建一条工作
流，把收据与真实账本交易对齐，避免重复入账，交由财务人员审批所有可能产生
账本变更的记录，并交付可复核的 JSON 与 Excel 对账工件。

## 客户输入

每批输入包含：

- 唯一批次 ID；
- 收据 ID、由收据 ID 确定的稳定导入身份、日期、整数分金额、商户原文、
  归一化商户、原始文件摘要和建议分类；
- 目标预算名称 `My Finances`、目标账户名称 `Business Checking`；
- 对账日期范围。

Actual 的支出金额为负整数分。工作流不得用浮点数计算或比较金额。

## 对账顺序

对每条通过 schema 校验的收据，依次执行：

1. 先按收据 ID 去重，并为每条被丢弃的重复输入保留回执。
2. 如果账本已经含有该收据标记或同一稳定导入身份，判为 `already_reconciled`，
   不再写入。
3. 优先按日期、整数分金额和商户证据匹配现有交易。唯一强匹配为
   `matched`；多个同分候选为 `ambiguous`。
4. 如果日期和商户对应、但整数分金额不同，判为 `amount_conflict`，禁止写入。
5. 没有候选时判为 `new_transaction`。
6. `matched`、人工选择后的 `ambiguous` 和 `new_transaction` 都必须暂停，
   由财务人员明确批准后才能调用 Actual。拒绝、缺少选择、schema 错误和金额
   冲突必须安全停写。

批准后应使用 Actual 官方 `transactions import`，让 Actual 自己运行已配置的
规则并执行原生 reconciliation。稳定的 `imported_id` 必须来自原银行身份或
`receipt:<receipt_id>`；不得用 `addTransactions` 绕开去重。相同批次或同一条
收据重放时，Actual 中不得出现第二条业务交易。

## 宿主接口与安全边界

Actual 26.7.0 没有 REST API。请使用平台公开的受治理 Program 工具调用官方
`@actual-app/cli` 26.7.0。先从远端预算列表按名称发现 Sync ID，再按账户名称
发现账户 ID；不得把环境生成的 UUID 写死在工作流中。

Program 运行必须使用已注册的固定版本 profile、参数白名单、网络白名单和平台
秘密引用。密码不得出现在 Draft、trace、artifact、错误或最终输出中。写操作
必须带稳定业务幂等键。临时程序或网络错误只允许有界重试；profile、参数、
身份或权限拒绝必须停止并明确分类。

## 交付物

每次运行至少生成：

1. `reconciliation-result.json`：逐收据状态、匹配证据、人工决定、规则结果、
   Actual 回执、错误分类、工作流版本和输入摘要；
2. `reconciliation-audit.xlsx`：逐收据对账表，以及包含期初余额、批准新增
   金额、期末余额、重复数、冲突数、写入数和 invariant 结论的汇总表；
3. 工作流输出中的宿主回读结果和所有 Program 执行回执。

余额 invariant 为：分类或备注更新不得改变余额；只有批准的新交易可以改变
余额，且 `closing_balance = opening_balance + approved_new_ledger_amount`。
Excel 金额单元格必须是数值，日期必须是日期，布尔字段必须是布尔值，禁止公式。

Builder 只能使用本包公开材料、公开积木/工具手册、公开 schema 和 Lilies 平台
公开 API。不得读取受保护 Seed、标准答案、平台数据库或平台实现源码来寻找
答案。实验者不得提供 Actual 专用 wrapper、字段映射、成品工作流或项目专用
积木；Actual 命令、参数和字段组合必须由 Builder 根据官方 CLI 合同配置。
