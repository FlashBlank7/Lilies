# asset_adaptive_default_live_acceptance_boundary

## 结论

`adaptive` 被产品化为 omitted `reuse_depth` 默认策略之后，需要把“默认路径接线正确”和“默认路径 live 可靠”分开讨论。`v0.2.53` 的真实 acceptance 已证明省略 `reuse_depth` 会走到 `policy_default -> adaptive -> deep`，而且元数据完整可见；但同一条链路仍可能因为 provider timeout 在 mandatory test 收口前进入 `needs_attention`。

## 获得成本

- 一轮 canonical E05 runner 改造
- 一轮确定性回归测试
- 一轮 bounded paid/live acceptance
- 一份 raw JSON、summary 和 DOCX 报告

## 证据链

- `docs/experiment-status/evidence/experiment_v0.2.53_e05_data_analyzer_policy_default_live_2026_07_10.json`
- `docs/experiment-status/evidence/experiment_v0.2.53_e05_data_analyzer_policy_default_live_2026_07_10_summary.md`
- `docs/experiment-status/reports/2026-07-10_0720_E05_policy_default_live_acceptance.docx`

## 适用边界

- 适用于讨论 Builder/API omitted-depth 默认策略是否已经“接通”。
- 适用于决定是否要继续保留 `reuse_depth` override 和回退边界。
- 不适用于声称 adaptive default 的 live reliability 已经闭环。

## 复用方式

- 在 stage planning 中，把“默认路径正确性”与“默认路径稳定性”拆成不同任务。
- 在实验 ledger 中，把这类 acceptance 结果标成 `验证应用 / 暴露可靠性缺口`，避免误写成“已关闭”。
- 在后续工程设计中，优先把修复目标写成 timeout / mandatory-test 收口，而不是重新争论 fixed-depth 默认值。

## 禁止滥用

- 不要把 `policy_default` 元数据正确返回，误写成默认路径稳定可用。
- 不要因为 `adaptive` family 之前表现好，就跳过 omitted-depth 的真实 acceptance。
- 不要用未完成 mandatory test 的 live result 直接证明产品默认已经工业可用。
