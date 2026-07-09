# E05 Template Reuse Depth Ledger

状态：多轮 paid/live 完成并触发工程修复；adaptive reuse-depth policy 已完成双 family live validation 并已产品化；policy-default live acceptance 已完成但暴露默认路径可靠性缺口

## 当前结论

Template reuse 已证明会改变 Builder 行为，也证明 marketplace expandability contract、timeout boundary、build deadline 和 customer-support guardrails 是必要工程边界。当前证据不支持“reuse depth 越深越好”，但已经足以回答更重要的问题：固定 `shallow` 不是跨任务族都稳定的默认候选，而 adaptive 已经拥有足够的 paid/live 证据成为默认 Builder suggestion mode 的候选。`v0.2.49` 的 `data_analyzer` slice 给出 deep-resolving live evidence；`v0.2.51` 的 `code_review` slice 给出 shallow-resolving live evidence；`v0.2.53` 则进一步证明 omitted `reuse_depth` 的 product-default 路径确实会走到 `policy_default -> adaptive -> deep`。因此，E05 关于“默认建议应该怎么定”的核心问题已经闭环；当前剩余问题不再是默认策略是否正确，而是默认策略在 live Builder 链路里的可靠性是否足够稳定。

## 关键证据

| 切片 | 标记 | 报告 | 默认摘要 |
| --- | --- | --- | --- |
| artifact review | 未关闭 | `../reports/2026-07-09_1809_E05_template_reuse_depth_artifact_review.docx` | `../evidence/experiment_v0.2.29_formal_tranche_summary_2026_07_09.json` |
| first live comparison | 已应用窄修复 | `../reports/2026-07-09_2051_E05_template_reuse_depth_live_comparison.docx` | `../evidence/experiment_v0.2.38_e05_template_reuse_depth_2026_07_09_summary.md` |
| expandability fix validation | 验证应用 | `../reports/2026-07-09_2305_E05_after_expandability_fix_validation.docx` | `../evidence/experiment_v0.2.39_e05_after_expandability_fix_2026_07_09_summary.md` |
| timeout success condition | 验证应用 | `../reports/2026-07-09_2311_E05_success_condition_after_timeout_boundary.docx` | `../evidence/experiment_v0.2.41_e05_success_condition_2026_07_09_summary.md` |
| customer-support second family | 验证应用 | `../reports/2026-07-10_0024_E05_customer_support_reuse_depth.docx` | `../evidence/experiment_v0.2.43_e05_customer_support_2026_07_09_summary.md` |
| customer-support rerun | 验证应用 | `../reports/2026-07-10_0103_E05_customer_support_rerun_after_guardrails.docx` | `../evidence/experiment_v0.2.45_e05_customer_support_rerun_2026_07_10_summary.md` |
| customer-support deep governance closure | 已应用/验证应用 | `../reports/2026-07-10_0302_E05_customer_support_deep_teammate_governance.docx` | `../evidence/experiment_v0.2.46_e05_customer_support_deep_only_teammate_governance_2026_07_10_summary.md` |
| data-analyzer breadth/default check | 验证应用 / 默认假设修正 | `../reports/2026-07-10_0420_E05_data_analyzer_breadth_default_policy.docx` | `../evidence/experiment_v0.2.47_e05_data_analyzer_breadth_2026_07_10_summary.md` |
| adaptive policy deterministic backtest | 已应用/验证应用 | `../reports/2026-07-10_0434_E05_adaptive_reuse_policy_backtest.docx` | `../evidence/experiment_v0.2.48_e05_adaptive_reuse_policy_backtest_2026_07_10_summary.md` |
| adaptive live validation (`data_analyzer`) | 验证应用 | `../reports/2026-07-10_0452_E05_adaptive_live_validation_data_analyzer.docx` | `../evidence/experiment_v0.2.49_e05_data_analyzer_adaptive_live_2026_07_10_summary.md` |
| adaptive live validation (`code_review`) | 验证应用 / 默认化门槛通过 | `../reports/2026-07-10_0530_E05_code_review_adaptive_live_validation.docx` | `../evidence/experiment_v0.2.51_e05_code_review_adaptive_live_2026_07_10_summary.md` |
| adaptive default live acceptance (`data_analyzer`) | 验证应用 / 暴露可靠性缺口 | `../reports/2026-07-10_0720_E05_policy_default_live_acceptance.docx` | `../evidence/experiment_v0.2.53_e05_data_analyzer_policy_default_live_2026_07_10_summary.md` |

## 已应用工程

- v0.2.39：Builder `template_list`/`template_expand` 支持 marketplace Template，并返回 `source`。
- v0.2.40：provider timeout 能被 runtime event、Builder event、Platform Harness metadata 记录。
- v0.2.42：Builder build request 支持 whole-build deadline。
- v0.2.44：E05 result JSON 语义拆分，`template_expand` 返回合同/验证摘要，Builder 阻止删除 mandatory test 仍依赖的唯一 required node type。
- v0.2.46：Builder teammate work受 repair budget 和剩余 build deadline 共同约束；teammate-side `test_run` 达到 `maximum repair cycles reached` 后不再继续长尾 debug。
- v0.2.48：新增共享 adaptive template strategy helper；API/Builder `template_suggestions` 支持 `reuse_depth=adaptive`，并返回 `effective_reuse_depth`、`recommended_action`、`policy_reason`；确定性 backtest 产出作为首轮策略验证。
- v0.2.49：canonical E05 runner 原生支持 `adaptive` 臂；首个 live validation 显示 adaptive 在 `data_analyzer` family 中解析为 `deep` 且比 explicit `shallow`/`deep` 更快 `published`。
- v0.2.51：`code_review` second-family live validation 显示 adaptive 在 shallow-resolving family 中正确解析为 `shallow`，并以 `published`、`313.696s`、`38 / 46` model/tool calls 优于显式 `shallow` (`ready`, `388.427s`) 与显式 `deep` (`needs_attention`, `382.777s`, `44 / 70`)；由此形成 Builder 默认建议门槛。
- v0.2.52：API 与 Builder 在 omitted-depth 路径上正式默认到 `adaptive`；结果显式返回 `reuse_depth_source`、`defaulted_by_policy`、`default_policy_version` 与 `available_overrides`，把产品化默认与显式 override 清楚地区分开。

## 关键结果

- v0.2.38：`none` published 且最低成本；`shallow/deep` 因 mandatory tests 失败，触发 expandability contract 修复。
- v0.2.39：`deep` published，`shallow/deep` 均成功展开 marketplace `code_reviewer`。
- v0.2.41：`shallow` published 且成本最低；`deep` ready 但更贵；`none` 失败来自 provider timeout。
- v0.2.45：`none` published 且 benchmark pass；`shallow` ready；`deep` benchmark-clean 但 hit `BuildDeadlineExceeded`。
- v0.2.46：customer-support `deep` 在相同预算族下回到 `ready`，耗时从 `602.071s` 降到 `482.221s`；full-suite breadth 仍需独立关闭。
- v0.2.47：`data_analyzer` family 中 `none` runtime 失败、`shallow` benchmark-clean 但超时、`deep` `published` 且耗时 `461.068s`；固定 `shallow` 默认假设不再成立。
- v0.2.48：adaptive policy deterministic backtest 对当前三族给出 `exact_matches=2`、`bounded_matches=1`、`mismatches=0`；当前窄规则足以作为后端显式策略上线，但还不能替代 fresh live validation。
- v0.2.49：在 `data_analyzer` 的 bounded paid/live rerun 中，`shallow`、`deep`、`adaptive` 全部 `published`，但 adaptive 解析为 `deep` 后以 `159.669s`、`11/17` model/tool calls 收敛，快于 `shallow` (`213.959s`, `9/20`) 与 `deep` (`301.290s`, `15/23`)。
- v0.2.51：在 `code_review` 的 bounded paid/live run 中，`shallow` 为 `ready`、`deep` 为 `needs_attention`、`adaptive` 为 `published`；adaptive 解析为 `shallow`，并以 `313.696s`、`38/46` model/tool calls 获得该 family 的最优 operational outcome。
- v0.2.53：省略 `reuse_depth` 的 live acceptance 中，`policy_default` arm 明确返回 `reuse_depth_source=policy_default`、`defaulted_by_policy=true`、`effective_reuse_depth=deep`，证明产品默认路径接线正确；但该 arm 在 `186.714s` 时因 `model stream timed out after 180s` 进入 `needs_attention`，说明默认路径的 live reliability 仍需工程修复。

## 下一步

E05 的原始 backlog 问题已经从“该不该上 adaptive”转移到“adaptive 产品化后的 live reliability 与长期观测”。因此下一步不是再回到 fixed-depth 争论，而是优先修复 policy-default 路径在真实运行中暴露出的 timeout / mandatory-test 收口缺口，并继续保留 fixed-depth 显式选项与后续 family 监测。
