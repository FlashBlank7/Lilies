# E07 Complexity Router Ledger

状态：未开始

## 实验问题

能否根据需求难度自动选择 operator、workflow depth、模型、Builder Team 或 skill 链条，使简单任务低成本完成，复杂任务获得更长计划和更强模型支持？

## 当前证据

暂无正式实验报告、summary 或 raw evidence。

## 初始设计方向

- 定义 simple/medium/complex 三档需求。
- 为每档指定 builder policy：max turns、repair cycles、reuse depth、model tier、是否 plan-first。
- 比较 routing 前后的成功率、成本、超时率和人工修复率。

## 下一步

先用已有 E01/E05 结果抽取 routing 假设，再设计正式实验。不要直接把假设写入默认策略。
