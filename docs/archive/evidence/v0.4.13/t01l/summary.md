# V04-13-T01L 证据摘要

T01L 最终实现位于独立同级仓库 `../LiliesAgent/` 的提交
`fd0f1a4e67511157f805f936f58d5152fe67dc5f`。它只属于软件开发压力测试，
固定 `enterprise_denominator=false`，不计入六个企业项目的客户成功分母。

## 已实现

- broker、executor、resolver、reviewer 四个主体使用互斥权限；无人监管只取消
  逐消息人工监控，不允许 executor 铸造、批准、修复或扩大自己的授权。
- 冻结 assignment 把工作区、允许路径、命令及可执行文件摘要、参数模板、网络、
  副作用、deadline、模型和 Token/成本/工具/进程/文件预算纳入同一 scope digest。
- 文件工具使用非阻塞 no-follow descriptor、单链接边界、认证 state snapshot
  检测和有界 worker；文件发布进入原子 publication fence，发布阶段超时或取消
  只能进入 `reconciliation_required`。
- 进程只接收 broker 创建的只读、内容寻址 FD 快照，不获得 workspace pathname
  或文件写权限；静态 macOS loader profile 排除 `/usr/local`、虚拟环境和动态
  toolchain root。输入快照和保留输出进入同一 byte ledger。
- 每个进程使用稳定、`0500` 且在 prepare 与执行前两次确认为空的私有 cwd。
  leader 先停在 nonce gate，完整 leader/watchdog 身份持久化后才能执行；
  watchdog 独立执行 wall timeout、输出上限、guardian EOF 和完整进程组回收。
- 文件、进程和模型副作用都先持久化 `prepared`。重启或用量不确定时进入
  reconciliation/unknown，稳定幂等键和 payload 绑定阻止重复变更。
- 确定性模型—工具循环真实产生源码变更、内容寻址 diff、源码检查、受限进程测试
  和按 stage/model 记录的 Token 用量；Provider 关闭时在准备与网络请求前失败。

## 验证结果

- 独立仓库 Python 全量：`322 passed`；合同聚焦独立复核：`147 passed`。
- 真实跨进程 CLI 与确定性 provider 的 diff/test/usage 聚焦：`2 passed`。
- Ruff、compileall、生成契约检查、`git diff --check`：全部通过。
- macOS Swift：`99 passed`；`swift build -c release` 通过。
- `build_and_run.sh --verify` 已验证精确 `LiliesWork.app` 二进制、独立 daemon
  身份和 `model_egress_enabled=false`。
- 付费模型调用为 0，实际账单为 0 美元；确定性 fixture 用量不是供应商账单。
- 全新上下文 Closure Audit 从 Program Charter、锁定 Stage Contract、提交 diff
  和最终验证反向审查，结论 `PASS`、mandatory blocker `0`。

完整机器可读结果见 `verification.json`，脱敏命令序列见
`independent-cli-transcript.json`，最终只读审查见 `closure-review-pass.md`。
本目录不会归档 bearer、一次性配对码、bootstrap credential、模型 Key、SQLite
数据库或原始未脱敏 transcript。

## 声明限制

证据上限是 controlled-local macOS integration 与 deterministic provider。
不宣称 live DeepSeek 开发质量、Android、Windows、生产隔离、企业客户结果，
也不把 build/launch 冒充原生视觉、键盘、VoiceOver 或真人交互通过。四个由淘汰
pre-watchdog 实验留下的不可中断宿主进程仍需主机重启清理；它们不是模型工作流，
没有 Provider 出口或 Token 调用。最终实现的完整回归未产生新的已知残留。
