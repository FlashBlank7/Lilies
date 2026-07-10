# E10 Assistant Memory Surface Ledger

状态：governed_surface_contract_implemented_pending_runtime_productization

## 实验问题

Lilies 是否应具备类似助手的多天记忆、活动轨迹检索和文件系统封装能力？如果做，权限、审计、撤销、隐私和 memory store 边界如何设计？

## 当前证据

v0.2.2 曾明确延期该方向，原因是权限、审计、撤销、文件系统边界风险高。v0.2.57 deterministic boundary fixture 确认 unrestricted assistant memory 不允许；governed memory surface 至少需要 permission scope、audit log、revoke、retention policy 和 source attribution。

证据：`../evidence/experiment_v0.2.57_full_backlog_closure_2026_07_10_summary.md`

v0.2.136 已接受受治理记忆的产品边界：permission scope、audit log、revoke、retention policy、source attribution 和 no unrestricted filesystem memory 均为强制控制。该版本只关闭“边界未定义”的 blocker，不声明 memory surface 已实现。

证据：`../../workingon-archives/v0.2.136/boundary_v0.2.136_e10_governed_memory_summary.md`

v0.2.137 已实现 governed memory surface contract：后端 service 支持 permission-scoped create/read/update/revoke/expire，持久化 `governed_memory_items`，为每次操作写入 append-only audit event，并暴露 `/api/v1/platform/governed-memory` 最小 API surface。该版本仍不声明 runtime assistant retrieval、Studio UI 或 unrestricted filesystem memory。

证据：`../../workingon-archives/v0.2.137/evidence_v0.2.137_e10_governed_memory_surface_contract_summary.md`

## 下一步

进入 `v0.2.138_e10_runtime_memory_retrieval_integration`：将 governed memory 以显式 opt-in、scope-bound、audit-backed 的方式接入 runtime retrieval 或等价产品路径。不得把 unrestricted memory、后台任意文件系统索引或无审计长期记忆作为产品路径。
