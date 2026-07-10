# E02 Readable TestFrame Ledger

状态：completed_for_proxy_blocked_for_true_human_panel

## 当前结论

`readable_report` 和 TestFrame 能显著改善 reviewer proxy 的错误定位与修复建议质量。当前证据支持将 readable TestFrame 作为默认审阅层，raw JSON 作为 debug 展开层。但实验没有真实人类计时数据，不能宣称人工审阅时间已被证明下降。

## 证据

| 项目 | 路径 |
| --- | --- |
| 工程证据审查报告 | `../reports/2026-07-09_1809_E02_readable_testframe_artifact_review.docx` |
| proxy 对照报告 | `../reports/2026-07-09_2013_E02_readable_testframe_reviewer_proxy.docx` |
| 默认摘要 | `../evidence/experiment_v0.2.36_e02_readable_testframe_review_2026_07_09_summary.md` |
| raw evidence | `../evidence/experiment_v0.2.36_e02_readable_testframe_review_2026_07_09.json` |
| stage report | `../../stage-reports/v0.2.36_e02_readable_testframe_human_review.md` |

## 关键结果

- deterministic estimated JSON paths：raw `18`，readable `10`。
- paid reviewer proxy：raw score `0.375`，readable score `1.0`。
- provider/model：`deepseek-v4-pro`。

## 应用记录

标记：验证应用。工程上可把 readable report 作为 tester/reviewer 默认展示层，但 raw JSON 保留为争议和调试证据。

## 下一步

v0.2.57 final disposition：Readable TestFrame is validated as the default reviewer surface; true human timing claims remain externally blocked. 若要证明真实人工审阅耗时下降，仍需 recruited participants 和 timing protocol，不能由自动化替代。

证据：`../evidence/experiment_v0.2.57_full_backlog_closure_2026_07_10_summary.md`
