# asset_adaptive_reuse_defaultization_gate

## 1. 核心结论

Lilies 可以把 `adaptive` 提升为默认 Builder suggestion mode，但只能在一个明确的门槛下这样做：

1. 至少要有两个 bounded paid/live family。
2. 两个 family 必须互补：
   - 至少一个 shallow-resolving family；
   - 至少一个 deep-resolving family。
3. 在这两个 family 上，adaptive 必须同时满足：
   - 解析到可解释的 concrete depth；
   - benchmark-clean；
   - 最终 operational outcome 不弱于最强 fixed arm；
   - 成本或耗时至少不显著更差。

截至 `v0.2.51`，这个门槛已经达到：

- `data_analyzer`：adaptive -> `deep`，`published`，快于 fixed `shallow/deep`。
- `code_review`：adaptive -> `shallow`，`published`，优于 `shallow=ready` 与 `deep=needs_attention`。

因此，当前结论是：

- **可以**把 adaptive 用作默认 suggestion mode。
- **不可以**删除 fixed-depth 显式选项。
- **不可以**把这个结论夸大成“adaptive 永远最优”。

## 2. 获得成本

这个资产不是 prompt 直觉，而是经过以下高成本链条形成的：

- 多轮 E05 paid/live 对照；
- marketplace Template expandability 修复；
- timeout / build deadline / teammate governance 等工程边界补齐；
- adaptive deterministic backtest；
- 两个互补 family 的 paid/live validation。

重新获得这套结论的成本高，因此值得沉淀为少而精的智力资产。

## 3. 证据链

- `docs/experiment-status/evidence/experiment_v0.2.48_e05_adaptive_reuse_policy_backtest_2026_07_10_summary.md`
- `docs/experiment-status/evidence/experiment_v0.2.49_e05_data_analyzer_adaptive_live_2026_07_10_summary.md`
- `docs/experiment-status/evidence/experiment_v0.2.51_e05_code_review_adaptive_live_2026_07_10_summary.md`
- `docs/experiment-status/reports/2026-07-10_0452_E05_adaptive_live_validation_data_analyzer.docx`
- `docs/experiment-status/reports/2026-07-10_0530_E05_code_review_adaptive_live_validation.docx`
- `docs/experiment-status/ledgers/E05_template_reuse.md`

## 4. 适用边界

适用于：

- 判断 Builder 默认 suggestion mode 是否应该从 fixed `shallow` 转向 `adaptive`。
- 设计 adaptive 上线时的产品文案、监控和回退条件。
- 审阅新的 family 结果时快速判断它是在挑战 adaptive，还是只是 long-tail 监测样本。

不适用于：

- 证明 adaptive 可以替代所有 fixed-depth 显式控制。
- 证明任何任务族都必须走 adaptive。
- 代替后续 family 退化监测。

## 5. 复用方式

在做 adaptive 产品化前，先检查这四件事：

1. 是否已经同时拥有 shallow-resolving 和 deep-resolving live family。
2. 是否两者都 benchmark-clean。
3. 是否 adaptive 的最终 build outcome 不弱于最强 fixed arm。
4. 是否仍保留用户或实验脚本显式指定 `none/shallow/deep` 的能力。

如果四项都满足，可以把 adaptive 作为默认 suggestion mode 上线；否则继续保留为显式实验选项。

## 6. 禁止滥用场景

- 不要把“adaptive 可默认建议”写成“adaptive 普遍优于一切 fixed depth”。
- 不要因为门槛通过，就删除 fixed-depth 控件、API 参数或实验臂。
- 不要把单个新 family 的坏结果直接当成 adaptive 失效；先判断它是在挑战门槛，还是在暴露新的 family 细分条件。
