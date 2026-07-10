# E10 Assistant Memory Surface Ledger

状态：blocked_until_governed_boundary

## 实验问题

Lilies 是否应具备类似助手的多天记忆、活动轨迹检索和文件系统封装能力？如果做，权限、审计、撤销、隐私和 memory store 边界如何设计？

## 当前证据

v0.2.2 曾明确延期该方向，原因是权限、审计、撤销、文件系统边界风险高。v0.2.57 deterministic boundary fixture 确认 unrestricted assistant memory 不允许；governed memory surface 至少需要 permission scope、audit log、revoke、retention policy 和 source attribution。

证据：`../evidence/experiment_v0.2.57_full_backlog_closure_2026_07_10_summary.md`

## 下一步

Implementation remains blocked until the governed boundary is accepted as product scope. 不得在缺少边界设计时直接接入长期监控或文件系统封装。
