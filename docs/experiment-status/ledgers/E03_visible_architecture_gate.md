# E03 Visible Architecture Gate Ledger

状态：部分实现 / 部分验证

## 当前结论

Builder benchmark 已通过 `required_node_types`、`required_tool_nodes` 和节点等价规则防止黑箱 Agent 节点冒充可审计 BlockFlow。E01 complex required 也证明显式架构契约能提高结构覆盖。但还没有专门的 opaque-agent vs explicit-graph 对照实验。

## 证据

| 项目 | 路径 |
| --- | --- |
| Benchmark foundation | `builder_benchmark_foundation.md` |
| E01 complex required | `E01_plan_first_vs_node_by_node.md` |
| 相关 stage | `../../stage-reports/v0.2.5_builder_benchmark_suite.md` 到 `../../stage-reports/v0.2.9_benchmark_node_type_equivalence.md` |

## 边界

当前只能说架构可见性 gate 已在若干测试和实验中产生作用，不能说“可审计架构优于黑箱 Agent”已经被专门实验关闭。

## 下一步

设计相同需求下的 opaque `agent` 节点方案 vs explicit block graph 方案，对比可测试性、失败定位、修复成本和 benchmark 通过率。
