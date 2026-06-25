# 手动 Workflow Editor 测试计划

本文档用于系统性修复和防回归 Studio 手动编辑环节的 bug。目标不是只覆盖当前三个问题，而是把“可视化画布状态、节点配置、后端 Draft、测试发布门禁”之间的同步关系测清楚。

## 1. 当前已知问题与验收标准

### Bug 1：删除节点后再次创建节点，已删除节点重新出现

现象：

- 创建新节点。
- 选中节点后按 Delete，或点击“删除节点”。
- 再创建其他节点或触发刷新。
- 之前删除的节点重新显示。

根因假设：

- React Flow 本地删除没有同步到后端 Draft。
- 或后端删除成功，但前端 `selected/configText/nodes` 仍保留旧状态。

验收标准：

- Delete 键删除节点后，后端 Draft 不再包含该节点。
- 该节点相关边同时被删除。
- 再创建新节点、刷新页面、切换 tab 后，已删除节点不再出现。
- 如果删除的是当前选中节点，节点检查器清空，不再显示旧配置。

### Bug 2：拖拽连接新的变量聚合器节点后，配置没有刷新

现象：

- 新建 Variable Aggregator。
- 从上游节点拖线连接到 Aggregator。
- 画布显示连线，但配置面板中的 `variables` 没有加入 `$ref`。

验收标准：

- 连接到 Variable Aggregator 后，`config.variables` 自动加入对应上游 `$ref`。
- 如果当前选中的是该 Aggregator，配置面板立即刷新。
- 重复连接同一上游，不产生重复 `$ref`。
- 如果 `variables` 中存在 `null` 占位，优先替换占位；否则追加。

### Bug 3：删除配置中的连接节点后，画布没有渲染变化

现象：

- 选中 Variable Aggregator。
- 在 JSON 配置中删除某个 `variables` `$ref`。
- 保存配置后，画布上的对应边仍然存在。

验收标准：

- 保存配置后，画布边与 `variables` 中的 `$ref` 同步。
- 删除 `$ref` 会删除对应输入边。
- 手工新增 `$ref` 会补齐缺失输入边。
- 刷新页面后，配置和画布保持一致。

## 2. 状态同步不变量

每次手动编辑都必须满足这些不变量：

1. 后端 Draft 是持久化事实来源。
2. React Flow 画布只展示 Draft 的投影；任何本地删除/连接都必须写回 Draft。
3. 当前选中节点必须来自最新 Draft；如果 Draft 中不存在该节点，必须清空选中态。
4. 节点配置中的 `$ref` 和画布数据边不能长期分叉。
5. 所有 Draft 写操作必须携带 `expected_revision` 和 `idempotency_key`。
6. 写操作成功后，必须重新读取 Draft 并刷新画布、配置面板和验证状态。
7. 人工编辑后，`tested_hash` 必须失效，不能发布未重新测试的草稿。

## 3. 测试分层

### 3.1 后端 Draft 单元/集成测试

位置：`tests/test_workflow.py`

必测用例：

- `add_node` 后 Draft 包含节点。
- 重复 `add_node` 同 id 返回错误。
- `remove_node` 删除节点并清理相关边。
- `remove_edge` 删除边后 Draft 不再包含该边。
- `update_node` 修改 config 后 revision 增加、tested_hash 清空。
- revision 冲突返回 409。
- idempotency key 重复提交返回相同结果，不重复写入。
- 删除不存在节点/边返回可诊断错误。

当前已补回归：

- `test_draft_manual_delete_keeps_nodes_and_edges_consistent`

### 3.2 前端纯逻辑测试

目标：把画布状态同步逻辑抽成可测试函数后覆盖。

建议后续新增：

```text
platform/frontend/lib/editorGraph.ts
platform/frontend/lib/editorGraph.test.ts
```

必测函数：

- 从任意 config 中递归收集 `$ref.node_id`。
- 连接 Variable Aggregator 时生成/追加/去重 `variables`。
- 删除边时从 Aggregator `variables` 移除对应 `$ref`。
- 保存 Aggregator config 时计算应删除/补齐的边。
- Draft 刷新时，选中节点存在则同步 config，不存在则清空选中态。

### 3.3 前端组件/交互测试

建议引入 Playwright 或 React Testing Library。优先 Playwright，因为当前问题发生在真实画布交互中。

核心场景：

1. 创建节点 → 选中 → 按 Delete → 创建新节点 → 断言旧节点不出现。
2. 创建 Start + Variable Aggregator → 拖线连接 → 打开 Aggregator 配置 → 断言 `variables` 包含 `$ref`。
3. 在 Aggregator 配置中删除 `$ref` → 保存 → 断言边消失。
4. 选中节点 → 后端刷新/构建事件更新 Draft → 配置面板展示最新 config。
5. 点击连接线 → 画布高亮该线 → 左侧面板保持当前 tab 和内容不被强制切换。
6. 点击连接线 → 按 Delete/Backspace → 断言边消失且刷新后不恢复。
7. 删除连接线后，目标节点配置中指向源节点的 `$ref` 必须同步移除。
8. 新建连接后立即删除，不能因为 React Flow 本地边 id 与后端边 id 不一致而报错。
9. 拖动节点 → 刷新页面 → 位置保持。
10. 删除边 → 刷新页面 → 边不恢复。
11. 添加同类型多个节点 → 每个节点 id 唯一，删除其中一个不会影响其他节点。
12. 保存非法 JSON → 不写 Draft，不破坏画布。
13. revision 冲突 → 自动 refresh，展示最新 Draft。
14. 切换中英语言 → 节点 id、配置、边不变。
15. 在 Safari/Chrome/Firefox 至少各打开一次 Studio，确认不依赖 `Object.groupBy` 等新浏览器 API 导致黑屏。

### 3.4 API + UI 联合测试

流程：

1. 使用 API 创建测试 Application。
2. 前端打开该 Application。
3. 通过 UI 编辑节点/边。
4. 用 API 读取 Draft，断言后端状态与 UI 操作一致。
5. 刷新页面，断言 UI 仍与后端一致。

这类测试专门防止“UI 看似删了，后端没删”。

### 3.5 真实工作流验收测试

每次编辑器大改后，至少跑一个真实工作流：

- Start
- Tool
- Variable Aggregator
- Claude Agent
- If / Else
- Template Transform
- Answer

验收：

- 可以手工新增、连接、配置、删除、再连接。
- 测试运行通过。
- 发布版本成功。
- 发布版本运行不受后续 Draft 编辑影响。

## 4. 当前三个 bug 的回归脚本草案

### 4.1 Delete 键删除节点

```text
Given 一个空 Draft
When 添加 variable_aggregator 节点 join
And 点击 join
And 按 Delete
And 再添加 answer 节点
Then 画布只有 answer
And GET /draft 的 nodes 不包含 join
```

### 4.2 连接变量聚合器刷新配置

```text
Given start 和 join 两个节点
When 从 start 拖线到 join
Then join.config.variables[0].$ref.node_id == "start"
And 如果 join 被选中，JSON 编辑器立即显示这个 ref
```

### 4.3 删除配置引用同步画布边

```text
Given start -> join 且 join.variables 包含 start ref
When 在 JSON 编辑器删除 start ref 并保存
Then 画布不再显示 start -> join
And GET /draft 的 edges 不包含该边
```

## 5. 修复后的手工验收清单

每次修复后手工走一遍：

- [ ] `./scripts/dev_platform.sh --check-env`
- [ ] `./scripts/dev_platform.sh`
- [ ] 打开 `http://127.0.0.1:3000`
- [ ] 创建测试应用。
- [ ] 添加 3 个节点。
- [ ] 选中节点并按 Delete。
- [ ] 刷新页面，确认节点不恢复。
- [ ] 添加 Variable Aggregator。
- [ ] 拖线到 Aggregator，确认配置刷新。
- [ ] 删除 Aggregator 配置中的 `$ref` 并保存，确认边消失。
- [ ] 点击画布中的连接线，确认只有线高亮，左侧面板不切换 tab、不覆盖当前内容。
- [ ] 选中线后按 Delete/Backspace 删除，刷新页面后确认不恢复。
- [ ] 删除线后，检查被连接的目标节点配置中不再残留对应 `$ref`。
- [ ] 新建线后立刻删除，确认不出现 edge not found / 404 一类错误。
- [ ] 运行测试。
- [ ] 发布版本。

## 6. 每次 Debug 后必须更新的材料

如果启动、调试或编辑方法发生变化，必须同步更新：

1. `README.md`：复制即可运行的启动/验证命令。
2. `docs/MANUAL_EDITOR_TEST_PLAN.md`：新增 bug 的复现、验收和回归场景。
3. `PRELOAD_PROMPTS.md`：把本次 debug 方法论沉淀为后续预加载 prompt。

这条规则是为了避免“代码修好了，但下一轮 agent 又按旧方法启动/调试”的问题。
