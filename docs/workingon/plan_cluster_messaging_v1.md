# plan_cluster_messaging_v1

## 1. 状态：✅ 已完成 (2026-07-23)

基于 The Pair 理论的 Agent 集群协作基础设施——6 个新积木 + SQLite 消息总线 + 注册中心 + 冲突检测器——的完整实现。

## 2. 设计依据

- 设计文档：`docs/current-design/design_cluster_messaging_v1.md`
- 理论依据：`docs/intellectual-assets/asset_harness_llm_composite.md`（Harness+LLM 复合体是唯一复用单元）
- 边界约束：`docs/intellectual-assets/asset_platform_harness_task_monitor_boundary.md`

## 3. 交付清单

| 交付物 | 文件 | 状态 |
|--------|------|------|
| 消息总线基础设施 | `platform/backend/src/agent_platform/cluster_messaging.py` (335 行) | ✅ |
| 积木定义 | `platform/backend/src/agent_platform/cluster_blocks.py` (160 行) | ✅ |
| 运行时集成 | `platform/backend/src/agent_platform/workflow_runtime.py` (+90 行) | ✅ |
| 积木注册 | `platform/backend/src/agent_platform/blocks.py` (+8 行，51 积木总数) | ✅ |
| API 端点 | `platform/backend/src/agent_platform/api.py` (+50 行，4 个端点) | ✅ |
| 设计文档 | `docs/current-design/design_cluster_messaging_v1.md` | ✅ |
| 智力资产 | `docs/intellectual-assets/asset_cluster_pair_architecture.md` | ✅ |

## 4. 关键指标

- 新积木：6 个（cluster_publish, cluster_subscribe, cluster_register, cluster_discover, cluster_acquire, cluster_release）
- 新增文件：2 个（cluster_messaging.py, cluster_blocks.py）
- 新增 API：4 个（/api/v1/cluster/*）
- 后端积木总数：45 → 51
- 零外部依赖（纯 SQLite + asyncio）
- 零破坏性变更
- 功能验证：7/7 项通过

## 5. 功能验证证据

```
✅ 注册中心：3 个 Agent 注册，capability 查询返回正确结果
✅ 消息总线：5 条消息发布，订阅者按序接收，独立游标
✅ 幂等发布：相同 msg_id 返回原消息（priority 不变）
✅ 多订阅者：agent-executor 和 agent-backup 各自独立接收
✅ 冲突检测：写锁阻止后续写/读，释放后可重新获取
✅ 后端启动：51 blocks，status=ok，deepseek=True
✅ API 端点：4 个集群监控端点正常响应
```

## 6. 实现决策记录

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 消息存储 | Redis / SQLite / JSONL | SQLite | 零外部依赖，Lilies 已使用 SQLite WAL |
| 积木注册 | 新文件 / 合并到 blocks.py | 分离 cluster_blocks.py | 关注点分离，遵循 block_families 先例 |
| 消息投递 | Push / Pull | Pull（订阅者主动拉取） | 避免推送失败处理，简化故障模型 |
| 冲突策略 | 悲观锁 / 乐观锁 | 悲观锁（前置获取） | Agent 协作场景更适合预防式冲突避免 |
| API 路由 | /cluster/... | /api/v1/cluster/... | 与现有 REST 命名一致 |

## 7. 后续待办（下一阶段）

- [ ] 大规模并发压力测试（100+ Agent 同时发布/订阅）
- [ ] cluster_mailbox 积木（合并 publish + subscribe 的便捷积木）
- [ ] 消息 TTL 和自动清理策略
- [ ] Agent 心跳检测（registry 的 status 从 active → offline 的自动转换）
- [ ] 分布式消息总线升级路径（SQLite → Redis Streams / NATS）
- [ ] Builder 模板：预置多 Agent 协作模板（如 Map-Reduce 模式、Scatter-Gather 模式）
