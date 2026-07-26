# V04-13-T01K 证据摘要

状态：全新只读 Closure Audit 已判定
`PASS at the scoped evidence floor`，`V04-13-T01K` 完成。本任务固定
`enterprise_denominator=false`，不替代六个企业项目的 T01H 成功分母。

T01K 已把莉莉丝解耦为平台上一级的独立
`/Users/zhonghaoyang/Code/agent/LiliesAgent` Git 仓库。该仓库拥有自己的
Python distribution、daemon、CLI、私有状态、测试、版本化客户端合同和
SwiftPM macOS 原生应用；运行时不导入 `agent_platform`，不共享平台数据库或
平台 API Token。平台边界只保留认证的 loopback HTTP。

安全发现与配对已经失败关闭：平台只接受同一用户、私有目录、mode 0600、
普通非符号链接且在读取期间身份稳定的 discovery record，核对 loopback
listener PID、distribution id 和公共 health 指纹；discovery 本身不授权。
授权必须经过显式、一次性、有期限且 scope 精确匹配的配对码，平台从不读取
daemon bootstrap secret。

模型出口默认关闭。真实认证观测在 5.709 秒的双快照窗口内记录到 Token、
调用、usage record、unknown call 和成本增量全部为 0，启动自动消费源为 0，
因此对被检查的平台和独立 daemon 可声明
`safe_now=true`、`safe_on_platform_or_daemon_start=true`。该结论不是
OpenAI/Codex 账户级账单监控，也不覆盖其它会话、机器、容器或未分类程序。

Token 账本现在以 daemon-global receipt 为权威，并可按 session、stage、
model 输出明细。七个旧 settled turn 只持久化了 123 次聚合调用，因此升级时
创建 123 条 unknown call，绝不反推或估算历史 Token、成本、模型、stage 或
session；后续新调用会持久写入真实维度。

最终验证包括：

- 独立 Python：`231 passed, 9 skipped`，Ruff、格式、生成合同和 diff check
  通过。
- 平台 T01K 精确回归：`352 passed`，26 个相关文件 Ruff 和 diff check
  通过。
- macOS：`93 passed`，真实 daemon 重启集成、构建、严格 codesign 校验和
  精确打包进程启动通过。
- Python wheel/sdist 和 macOS ZIP 各自双构建字节一致；最终 wheel 在隔离
  Python 3.13 环境中完成真实 daemon、CLI 配对、usage 查询与安全停止。
- 真实平台通过公开 discovery 与一次性配对连接真实独立 daemon，认证全局
  receipt、ACL 明细、startup receipt 与空闲双快照均通过。

当前唯一与本地完成直接相关的证据债务是 `V0413-ED-004`：Computer Use
原生 pipe 两次相同启动失败，无法做精确打包应用的视觉、交互、键盘和
accessibility 检查。已达到 Swift 测试、构建、ad-hoc 签名和精确进程启动
层级，但不声明任何视觉或人工交互通过；该债务留给 T01J 在 provider 可用或
用户提供明确视觉确认时复查。Developer ID、notarization、公共 Gatekeeper
和跨机器分发也未声明。

证据入口：

- `distribution-manifest.json`
- `runtime-bridge-and-observability.json`
- `security-and-token-audit.json`
- `environment-and-claim-ceiling.json`
- `deterministic-tests.txt`

全新只读审计的 mandatory blocker 为 0；登记 manifest 后，当前任务改为
`V04-13-T01L`。
