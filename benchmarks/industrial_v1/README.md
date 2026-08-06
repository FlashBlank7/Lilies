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
4. **验收两层**——`structural`（输出字段存在性，runner 自动判）+ `review_notes`（人审要点）。

## 跑法

```bash
python3 scripts/run_benchmark.py                 # 全部六题（消耗真实 token）
python3 scripts/run_benchmark.py --task T1-procurement-reconciliation
python3 scripts/run_benchmark.py --skip-run      # 只构建不试跑
```

每次运行在 `results/<时间戳>/` 留下逐题报告；构建过程可在 Studio 的
「莉莉丝会话」面板或 `GET /api/v1/builds/{id}/transcript` 实时查看。

改进循环：**跑题包 → 读 transcript 定位 → 修平台/提示词 → 重跑对比**。
