# 工业题包 v1

六个任务，一一对应[产品北极星](../../docs/PRODUCT_NORTH_STAR.md)的六个工作流族，
取向承接旧战役的 EXP-LILIES-001..006（见 `docs/archive/experiments/lilies-collaboration/`）。

| 任务 | 族 | 旧题参照 |
|---|---|---|
| T1 采购对账 | 记录接入与结构化交付 | EXP-001 |
| T2 售后知识问答 | 企业 RAG（权限+引用+拒答） | EXP-002 |
| T3 告警分诊 | 混合智能流程自动化（人工确认） | EXP-003 |
| T4 预测性维护 | 工业 ML 推理与复核 | EXP-004 |
| T5 银行核销 | 结构化数据与工件交付 | EXP-005 |
| T6 补货计划 | 预测优化与规划 | EXP-006 |

## 设计原则（吸收 v1-v4 四次真实构建的教训）

1. **零外部凭据**——所有数据以运行时输入提供，任何任务都不需要真实 API key。
2. **客户口吻**——需求文本是业务人员的原话，不含平台术语。
3. **数字必须可复算**——凡涉及对账/统计/规划的任务，确定性计算是验收的一部分，LLM 只负责解释和建议。
4. **验收三层**（2026-08-23 升级，见下）——字段存在性 + 样例算对 + **未见输入算对**。

## 验收为什么从两层改成三层

原来的两层是 `structural`（字段存在性，机器判）+ `review_notes`（人审要点）。
日报基准的事故证明这不够：

- 只判字段存在，**每个数都算错也全绿**——形状合法的垃圾照样"通过"；
- 只用样例输入验收，**把样例答案硬编码进公式**同样全绿（真发生过）；
- 更要命的是，题目本身可能**在积木层无解**（公式引擎当时无法对对象数组分组求和），
  于是模型的"失败"里有一半是无解考卷的必然——测出来的结论是假的。

现在每道题多两样东西，都放在 [`reference.py`](reference.py)：

| | 是什么 | 挡住什么 |
|---|---|---|
| **参照解** | 一份独立的纯 Python 实现（不 import 任何平台模块）| 题目无解 / 判卷方与被判方共用一份代码 |
| **照妖镜输入** | 一组验收中从未出现的输入 + 同一实现算出的期望值 | 把样例答案硬编码进工作流 |

判卷器（`CHECKERS`）只核对**事实**，不规定输出条目的结构——明细长什么样是实现
自由，硬套 schema 等于把另一种同样正确的做法判错。宽进严出：结构随便，数字必须对。

`tests/test_industrial_benchmark_references.py` 守两件事：参照解算出的数与
`review_notes` 写的一致；判卷器面对正确输出零问题、面对典型错法一定报错。

> T2（权限内知识问答）不做数值参照解：它的正确性判据是"引用了哪几篇、拒答了没有"。

## 跑法

```bash
# 三层验收 + 四条件判卷（推荐）
python3 scripts/industrial_benchmark.py --task T1-procurement-reconciliation
python3 scripts/industrial_benchmark.py --all --builder mechanical

# 老的两层 runner（只判字段存在性，保留作对照）
python3 scripts/run_benchmark.py                 # 全部六题（消耗真实 token）
python3 scripts/run_benchmark.py --task T1-procurement-reconciliation
python3 scripts/run_benchmark.py --skip-run      # 只构建不试跑
```

`industrial_benchmark.py` 的四条件：C1 交付成立、C2 验收锚定具体值（只验形状不算）、
C3 样例输入算对、C4 未见输入算对。判卷结果落盘 `data/benchmark_runs/`。

每次运行在 `results/<时间戳>/` 留下逐题报告；构建过程可在 Studio 的
「莉莉丝会话」面板或 `GET /api/v1/builds/{id}/transcript` 实时查看。

改进循环：**跑题包 → 读 transcript 定位 → 修平台/提示词 → 重跑对比**。
