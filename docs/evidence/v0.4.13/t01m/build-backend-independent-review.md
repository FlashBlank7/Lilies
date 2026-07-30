# T01M 离线 Android 构建后端独立复审

- 任务：`V04-13-T01M`
- 后端标识：`android-sdk-java-offline-v1`
- 正式 LiliesAgent 提交：`d6f0ed7a493c213b6f654735e647011de93f73bc`
- 正式 LiliesAgent 树：`eb0b88221e1367e857c781427b27d027a7ce4a4a`
- 最终实现侧提交：`439f5c8b5648649c60cbe911e37a38b787aab005`
- 复审身份：`/root/backend_receipt_review`
- 最终结论：`PASS`

## 已确认的执行边界

- JBR 与 Android build-tools 使用精确文件 manifest，并在执行前复核路径身份；
  工具进程只能读取隔离 snapshot，不能写入工具链或项目原路径。
- 同 UID 的外部路径替换明确不在威胁模型内，receipt 固定记录
  `same_uid_external_path_replacement_resistant=false`，不会夸大成不可变路径。
- 输入使用只读 FD snapshot，目标进程在空私有 cwd 和 macOS Seatbelt 下运行；
  网络、项目脚本、注解处理、依赖下载和未授权命令均关闭。
- scratch 使用周期和结束 retained scan、每文件 `RLIMIT_FSIZE` 与十万 entry
  fail-close；明确记录无法证明瞬时峰值，而不是把 retained 数量冒充峰值。
- 每次构建的时间、I/O 与 scratch 预算都乘以 `reproducibility_runs=2`；
  只有两次独立干净快照字节一致的 APK 才能提升为内容寻址工件。
- 首次 API receipt、SQLite durable receipt、进程重启后的 GET 投影完全一致；
  replay 只改变顶层 `replayed`，不会丢失声明上限和 accounting 字段。

## 验证

- Android/Windows 集成定向测试：`113 PASS`
- 完整 Python 回归：`460 PASS`
- macOS Swift：`103 PASS`，`swift build PASS`
- Ruff、wheel 构建、隔离安装、CLI import/help smoke：`PASS`

## 声明上限

本复审证明后端实现、确定性合同和本机 macOS 隔离执行路径。它不证明尚未创建的
目标 Android 应用、APK、模拟器结果，也不抵抗同 UID 主体在调用期间替换外部
JBR 或 Android SDK 路径。该上限必须保留在每次 build receipt 中。
