# plan_V1.3_builder_quality_harness_and_visibility

## 1. 目标

V1.3 围绕两个主题：**可信度**（Builder 有测试兜底、进化链路有治理）和**可感知**（BlockFlow 可视画布、模板市场可浏览）。

具体交付：
- A1: Builder 测试基础设施（MockProvider + 单元测试 + 集成测试）
- A2: 进化任务治理（GovernedTask 状态机 + 取消 + 超时）
- A3: Builder benchmark v1（可量化的构建能力测试集）
- B1: BlockFlow 可视化画布 MVP
- B2: 模板市场只读页面

## 2. 范围

包含：
- 新增 `tests/test_builder.py` — Builder 单元测试和集成测试
- 新增 `agent_platform/testing.py` — 测试基础设施（MockProvider 等）
- 新增 `agent_platform/governed_task.py` — 受控异步任务
- 修改 `api.py` — 进化链路接入 GovernedTask
- 新增 `mobile_app/canvas.html` — BlockFlow 可视化画布
- 新增 `mobile_app/marketplace.html` — 模板市场页面
- 新增 `tests/test_builder_benchmark.py` — Benchmark 测试

不包含：
- Builder prompt 调优
- 新积木或新后端能力
- 前端拖拽编辑
- 模板创建/编辑 UI
- 跨框架导入导出

## 3. 关键决策

- MockProvider 基于 JSON 录制文件，模型可以精确回放已知的 tool_use 序列
- GovernedTask 不引入外部依赖，用 asyncio + 状态机自行实现
- 画布用纯 SVG + vanilla JS，不引入 React/Vue 框架降低依赖
- Benchmark 测试与单元测试分开，可以独立运行（耗时较长）

## 4. 实现路径

### A1: Builder 测试基础设施

1. 创建 `agent_platform/testing.py` — MockProvider 支持从 JSON 文件加载预录响应序列
2. 创建 `tests/test_builder.py`：
   - 测试 `_execute` 每个工具分支
   - 测试 `_agent_loop` 的完整构建流程（用 MockProvider）
   - 测试 template_expand 流程
   - 测试 manual_lookup 强制约束
   - 测试 repair 循环上限
   - 边界测试

### A2: 进化任务治理

1. 创建 `agent_platform/governed_task.py`
2. 修改 `api.py` 的 `_auto_extract_from_build` 接入治理

### A3: Builder benchmark v1

1. 创建 `tests/test_builder_benchmark.py`
2. 5 个递增难度的 benchmark 场景
3. 度量：结构正确率、首次成功率、修复轮次、节点数

### B1: BlockFlow 画布

1. 创建 `mobile_app/canvas.html`
2. SVG 渲染 DAG 图 + 积木面板 + 属性面板

### B2: 模板市场

1. 创建 `mobile_app/marketplace.html`
2. 卡片列表 + 搜索 + 分类过滤 + 评分展示

## 5. 依赖设计

- 引用 `docs/intellectual-assets/asset_platform_harness_task_monitor_boundary.md` — GovernedTask 设计
- 引用 `docs/intellectual-assets/asset_blockflow_language_system.md` — 画布术语
- 引用 `docs/intellectual-assets/asset_harness_llm_composite.md` — Harness+LLM 拆分原则

## 6. 验收标准

- [ ] 全量测试 61 passed → ≥ 80 passed（新增 Builder 测试和 benchmark）
- [ ] Lint clean on all changed files
- [ ] MockProvider 可以精确回放多轮 tool_use 序列
- [ ] Builder 测试覆盖 catalog/manual/template/draft/test/publish 全工具链
- [ ] GovernedTask 支持状态机、取消、超时
- [ ] _auto_extract_from_build 从裸 asyncio.create_task 升级为 GovernedTask
- [ ] 画布可以加载任意 BlockFlow 并渲染为 DAG 图
- [ ] 模板市场可以从 API 加载并展示卡片
