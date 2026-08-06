# E06 Small-model Translation Ledger

状态：completed_as_deterministic_fixture；real small-model run optional

## 实验问题

语言是否影响小模型理解 Builder/BlockFlow 任务？是否应先把中文需求翻译成英文中间表示，再交给小模型或低成本 builder arm？

## 当前证据

v0.2.57 deterministic fixture 已完成：structured English intermediate representation 覆盖 required slots `acceptance / inputs / outputs / required_nodes / test_frame`，direct Chinese fixture 覆盖 `0.6`，structured English IR 覆盖 `1.0`。

证据：`../evidence/experiment_v0.2.57_full_backlog_closure_2026_07_10_summary.md`

## 初始设计方向

- 对照组：中文原文直接生成。
- 实验组：中文原文 -> 英文结构化需求 -> 生成。
- 指标：build status、benchmark score、required node coverage、repair cycles、model/tool calls、人工可读性。

## 下一步

若未来引入低成本小模型 lane，可做真实 small-model run；当前原始 E06 已以 deterministic slot-coverage fixture 获得 final disposition，不能写成 paid/live 小模型结论。
