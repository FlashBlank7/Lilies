# T01M Android 项目迭代计划

状态：`candidate_pending_project_freeze`

本文件只拆分同一个独立 Android 项目“文明火种”的莉莉丝开发迭代，不是六个
客户工作流，也不进入企业成功分母。应用仓库始终是
`/Users/zhonghaoyang/Code/agent/LiliesCivilizationSeedAndroid`；平台仓库与
`LiliesAgent` 仓库不承载应用源码。

## 固定原则

- 六轮使用六个新的隔离 session、`DevelopmentAssignment`、run 和预算账本，
  但共享同一个 Android 项目 Git 历史。
- 每轮开始前冻结父 commit、tree 和工作区干净状态；每轮结束后保存模型调用、
  Token、工具、构建和 review 回执，再由外部控制器提交莉莉丝产生的原始 diff。
- 第一轮开始时父 commit 必须是空树 baseline
  `82424224a039a7af33189e1288717737e60d9706`；第一份非空 diff 只能来自莉莉丝
  的模型—工具循环。Codex 不创建工程骨架、应用源码、资源或测试。
- 每轮只允许本项目相对路径、冻结的文件读写工具和
  `android-sdk-java-offline-v1` 构建；网络、项目脚本、注解处理、下载、任意命令
  和模型自行扩权始终禁用。
- 每轮都执行主 APK 与 instrumentation 测试 APK 的冻结编译；`test_build`
  只证明测试 APK 编译，不冒充模拟器上的 `am instrument` 已执行。
- 模型用量只接受 Ollama 返回的 `prompt_eval_count` 与 `eval_count`。缺失、
  越界或无法对账立即停止，不估算为零；六轮精确硬上界与出口生命周期见
  `token-budget.zh.md`。
- 两轮连续没有可接受 diff、三轮连续产生相同构建指纹、达到任一预算、请求扩大
  路径/工具/网络/秘密/副作用，或工作区不再能归属到单一父 commit 时立即停止。

## 六轮顺序

### M01：空仓建基与序章

目标是由莉莉丝创建第一份非空项目：

- 根 `README.md`，中文在前；
- manifest、Java 源码目录、资源目录和测试源码目录；
- 暗紫科技主题、固定中文世界观和序章；
- 白发、闭眼、头发向两侧展开且服装完整的莉莉丝原创代码绘制或原创矢量资源；
- 启动“文明重建”并进入空火种库；
- 主 APK 和测试 APK 均可由冻结后端编译。

本轮不得声称 CRUD、持久化、完整无障碍或 oracle 已通过。

### M02：数据合同与本地持久化

在 M01 的已接受 commit 上实现：

- 火种数据模型、稳定 ID、创建序号和应用私有持久化；
- 新建、列表、默认优先级和“已复原 X / Y”；
- 合法 Unicode scalar、完整 Unicode 15 `White_Space`、NFC、code-point
  长度与固定排序合同；
- 对应的模型自写确定性测试。

本轮不得弱化题面中的 Unicode 或排序规则。

### M03：完整业务闭环

在 M02 的已接受 commit 上实现：

- 状态推进与回退；
- 搜索、类别筛选、状态筛选和清除；
- 编辑、取消删除、确认删除；
- 旋转、强制停止和重启后的数据恢复；
- 业务状态与持久化的确定性测试。

### M04：中文、视觉、无障碍与减少动态效果

在 M03 的已接受 commit 上完成：

- 所有默认用户界面、错误、空状态、确认框和无障碍名称以简体中文为主；
- `48dp × 48dp` 触控目标、冻结卡片语义 scope 与 TalkBack 顺序；
- `200%` 字体可达性；
- 四个冻结色值与暗紫科技视觉；
- 角色独立呼吸层，周期和幅度符合题面，背景/文案/按钮静止；
- 系统动画比例为零时角色和页面转场静止。

### M05：测试、安全与交付说明

在 M04 的已接受 commit 上补齐：

- 覆盖 Unicode 边界、CRUD、排序、恢复、减少动态效果和无障碍语义的测试源码；
- 无网络、无危险权限、无 WebView、无后台组件、无共享存储的 manifest 边界；
- README 中的构建、安装、离线、私有数据位置和验证说明；
- 两次独立干净快照的主 APK 可复现构建。

本轮仍不以模型自测替代外部 emulator oracle。

### M06：外部 oracle 驱动的收敛

外部控制器只向本轮提供 A01–A10 的失败摘要和公开运行观察，不提供隐藏实现答案、
应用源码分析或 oracle 内部秘密。莉莉丝只修复可复现失败；每个修复必须重新运行
主 APK 与测试 APK 构建。若干净模拟器在一次干净重建后仍连续两次失败，则停止并
保留失败证据，不通过扩大权限或绕过 oracle 继续。

## 每轮归档

每轮目录固定保存：

- `assignment.json`、scope digest、四角色 client ID 和 grant；
- run、event、model、Token、tool、build、permission 与 review 回执；
- 父 commit/tree、工作前状态、莉莉丝原始 diff、diff SHA-256；
- 接受或拒绝原因、接受后的 commit/tree 与干净状态；
- 主 APK/测试 APK 编译结果和内容摘要；
- 未知用量、reconciliation、停止条件与声明上限。

最终 Android runtime、截图、TalkBack、运动、对比度、包扫描和闭环证据属于项目
总 oracle，不得伪装成任一轮模型自测结果。
