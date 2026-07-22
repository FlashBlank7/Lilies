# plan_softblock_to_family_property

## 1. 目标

将 `soft_block` 从运行时积木类型降级为积木的元数据属性（`editor.family`），纠正"粒度放置错误"——家族分组是 UI/搜索层的概念，不应污染积木注册表的正交性。

## 2. 范围

**包含**：
- 从 `_AGENT_ARCHITECTURE_BLOCKS` 和 `_ZH_BLOCKS` 中移除 `soft_block`
- 移除 `SoftBlockConfig` Pydantic 模型
- `_definition()` 新增 `family` 参数
- 24 个 Agent 架构积木通过 `get_family()` 自动标注 family
- `workflow_runtime.py` 删除 soft_block 执行 case
- API `/soft-block/strategies` → `/blocks/families`

**不包含**：
- 前端积木目录按 family 分组显示
- Builder Team 的 family 维度积木搜索

## 3. 关键决策

- `FAMILY_MAP` 保留为纯数据（`soft_block.py`），不创建新的运行时抽象
- family 通过 `BlockDefinition.editor.family` 暴露，前端和 Builder 可在 UI/搜索层使用
- `get_family(block_type)` 提供反向映射（积木类型 → 家族名）

## 4. 实现路径

1. `soft_block.py`: 移除 SoftBlockConfig，保留 FAMILY_MAP + get_family() + get_strategy() + list_families()
2. `blocks.py`: _definition() 新增 family 参数，editor dict 包含 family
3. `workflow_runtime.py`: 删除 soft_block 运行时 case
4. `api.py`: 端点重命名

## 5. 依赖设计

- `docs/intellectual-assets/asset_harness_llm_composite.md` — "优化投向组合层，非积木层"
- ADR-001 — 积木粒度不合并

## 6. 验收标准

- [x] 全量测试 61 passed, 0 failed
- [x] Lint clean
- [x] 无代码引用 SoftBlockConfig
- [x] 24 个 Agent 架构积木均标注正确的 family
- [x] `/api/v1/blocks/families` 返回正确的家族分组
