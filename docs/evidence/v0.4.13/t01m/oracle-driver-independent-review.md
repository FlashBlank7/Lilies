# T01M 外部 Android 验收驱动独立复审

- 任务：`V04-13-T01M`
- 驱动仓库提交：`b6acc78d44f80f8def3a02c028d9663b6d59a75d`
- 驱动仓库树：`f031230c94833c0691084e49a1d8f91209f224fe`
- 驱动 APK SHA-256：
  `47f0568de6855f8bf893d76328cfacb1f354d0c7f4c416f224a35f649e893120`
- Oracle lock SHA-256：
  `3993b42f67479e80ee32ae872b3a544b32e31c111f08d9dcc2a7ea1c565775c1`
- 复审身份：`/root/android_backend_review`
- 复审方式：只读；没有访问目标应用仓库或目标 APK
- 最终结论：`PASS`

## 复审结论

- A07 编排顺序已证明为离线/清数据、daemon 前快照、强停与日志游标、启动前
  UID/netstats/socket/共享存储基线、完整 workload 区间 sampler、首次启动与
  完整流程、结束快照、daemon 后快照。
- canonical observability schema 对 snapshot/wrapper schema、capture、
  workload、invocation 时间戳和六个 ledger counter 均要求真正 JSON integer；
  `true`/`false` 不能利用 Python `bool` 的整数子类关系通过校验。
- attestation 中本来就应是布尔值的 `complete` 与 `read_only` 仍严格要求
  `true`，没有被整数修补误伤。
- before/after、六个 ledger 字段、schema 与全部时间戳均有布尔负例。
- `python3 -m unittest discover -s tests -v` 为 `54/54 PASS`；
  `python3 -m t01m_host --validate-config`、lock、host manifest 与
  `git diff --check` 均通过，工作树干净。

## 声明上限

本 PASS 只覆盖驱动源码、fixture/static 验证、锁、可复现驱动 APK 和
fail-closed schema。驱动复审期间没有目标应用源码或目标 APK，因此不声明
A01–A10 的目标运行时结果；这些结论只能由冻结 assignment 产生目标应用后，
在指定模拟器或设备上执行完整 oracle 得出。
