# E06 Small-model Translation Ledger

状态：未开始

## 实验问题

语言是否影响小模型理解 Builder/BlockFlow 任务？是否应先把中文需求翻译成英文中间表示，再交给小模型或低成本 builder arm？

## 当前证据

暂无正式实验报告、summary 或 raw evidence。

## 初始设计方向

- 对照组：中文原文直接生成。
- 实验组：中文原文 -> 英文结构化需求 -> 生成。
- 指标：build status、benchmark score、required node coverage、repair cycles、model/tool calls、人工可读性。

## 下一步

先做小规模 deterministic fixture，再做 bounded paid/live 或开源小模型对照。未产生 DOCX 前不得作为工程结论。
