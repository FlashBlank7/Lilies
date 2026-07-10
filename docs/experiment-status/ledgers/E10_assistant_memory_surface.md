# E10 Assistant Memory Surface Ledger

状态：governed_boundary_defined_pending_surface_contract

## 实验问题

Lilies 是否应具备类似助手的多天记忆、活动轨迹检索和文件系统封装能力？如果做，权限、审计、撤销、隐私和 memory store 边界如何设计？

## 当前证据

v0.2.2 曾明确延期该方向，原因是权限、审计、撤销、文件系统边界风险高。v0.2.57 deterministic boundary fixture 确认 unrestricted assistant memory 不允许；governed memory surface 至少需要 permission scope、audit log、revoke、retention policy 和 source attribution。

证据：`../evidence/experiment_v0.2.57_full_backlog_closure_2026_07_10_summary.md`

v0.2.136 已接受受治理记忆的产品边界：permission scope、audit log、revoke、retention policy、source attribution 和 no unrestricted filesystem memory 均为强制控制。该版本只关闭“边界未定义”的 blocker，不声明 memory surface 已实现。

证据：`../../workingon-archives/v0.2.136/boundary_v0.2.136_e10_governed_memory_summary.md`

## 下一步

进入 `v0.2.137_e10_governed_memory_surface_contract`：定义或实现 permission/audit/revoke/retention/source/no-unrestricted-filesystem-memory 的 surface contract。不得把 unrestricted memory、后台任意文件系统索引或无审计长期记忆作为产品路径。
