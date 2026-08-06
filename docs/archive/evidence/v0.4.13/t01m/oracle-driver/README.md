# T01M Android 外部黑箱驱动

这是 `V04-13-T01M` 的独立、只读黑箱驱动，不属于“文明火种”应用源码。
驱动源码在应用空基线仍为空树时冻结，只通过 Android 无障碍语义节点操作最终
APK，不读取应用仓库、私有数据或实现细节。

驱动提供以下通用动作：

- 按准确可见文字或 `contentDescription` 等待、点击及输入 Unicode 文本；
- 导出有界、稳定排序的无障碍树；
- 在驱动自身的私有沙箱保存无损 PNG 截图；
- 执行返回、滚动和 TalkBack 下一项滑动；
- 在不压制系统无障碍服务的模式下记录真实无障碍焦点事件；
- 导出逐 Unicode code point 的系统字符框；
- 为 R02–R14 同时绑定动作、无障碍事件、hierarchy 与 13 帧像素证据，并为
  onboarding / populated library 各捕获 26 帧静止证据。

宿主通过 `selector_base64` 和 `value_base64` 传递 UTF-8 Base64，驱动先做严格
UTF-8 解码，再调用 `ACTION_SET_TEXT`；这避免 shell 编码差异，也不会借用系统
剪贴板。ASCII 调试时仍可使用 `selector` / `value`。

非法 UTF-16 边界有一条完全隔离的 ASCII 通道：`set_text_utf16_hex` 只接受
`value_utf16_hex`，每个 UTF-16 code unit 必须是四位小写 hex。驱动直接构造
Java `char[]`/`String` 并以 `ACTION_SET_TEXT` 注入，再以小写 UTF-16 hex 回读
逐 code unit 比对。未配对 surrogate 绝不进入 UTF-8、JSON 文本或普通
instrumentation 文本回执；树和焦点证据会把这类值的普通文本置空，只保留
`*_utf8_valid=false` 与 ASCII `*_utf16_hex`。

重复动作通过 `scope_selector_base64` 先锁定卡片根或对话框文案。驱动从该语义
锚点的后代中查找目标；卡片 scope 绝不向祖先逃逸。只有精确匹配冻结删除提示
`删除“…”吗？此操作无法撤销。` 时，才允许在对话框祖先中查找“取消/删除”。
因此同屏重复动作不会退化成全局序号或坐标选择。

它不包含客户应用源码、项目专用字段映射、网络权限或隐藏答案。完整 A01–A10
流程由冻结的宿主控制器按公开 `acceptance-oracle.json` 编排。

## 冻结宿主控制器

`t01m_host` 是应用源码无关的 Python 标准库控制器。它只接受最终 APK、冻结
设备和黑箱语义输出，不读取目标仓库。先执行：

工具和冻结输入只通过环境身份解析，不在仓库中记录用户目录：

```sh
export T01M_ANDROID_SDK_ROOT=/path/to/android-sdk
export T01M_JBR_HOME=/path/to/jbr
export T01M_PROJECT_BRIEF_PATH=/path/to/project-brief.zh.md
export T01M_ACCEPTANCE_ORACLE_PATH=/path/to/acceptance-oracle.json
python3 -m t01m_host --validate-config
```

该命令只校验冻结配置、473 个原子 A06 步骤、Unicode fixture、driver 摘要和
`adb` / `aapt2` / `apkanalyzer` / `dexdump` 摘要；它不会寻找、安装或运行目标
APK，也不会把配置通过误报成运行通过。

目标 APK 存在后，先做绑定检查：

```sh
python3 -m t01m_host preflight --serial SERIAL --apk FINAL.apk
```

A01–A04 和 A05 是显式输入、失败即关闭的独立入口；控制器不会搜索目标仓库或
构建输出：

```sh
python3 -m t01m_host verify-a01-a04 \
  --repository /explicit/read-only/repository \
  --accepted-commit FULL_SHA \
  --assignment-ledger ledger.json \
  --build-receipt build.json \
  --rebuild-receipt-a rebuild-a.json \
  --rebuild-receipt-b rebuild-b.json \
  --output artifacts/a01-a04-result.json

python3 -m t01m_host run-a05 \
  --apk /explicit/final.apk \
  --output artifacts
```

A05 对每个 ZIP 中央目录项做两次独立读取，完整解析每个 root DEX 的 string/type/
proto/field/method/class ID 表、完整资源表和每个编译 XML，并输出
`package-analysis.json`、`apk-entry-inventory.json`、
`resource-inventory.json` 与 `dex-reference-scan.json`。

随后可在 API 37 的冻结 AVD 上运行一条干净数据 A06。该入口固定 100% 字体和
正常动画，不提供起止步参数；底层调试即使成功执行部分步骤也只能输出
`result=partial`，不能成为 A06 PASS：

```sh
python3 -m t01m_host run-a06 \
  --serial SERIAL \
  --apk FINAL.apk \
  --output /absolute/evidence/root
```

完整运行入口另有 `run-a07`、`run-a08`、`capture-normal-motion`、
`run-reduced-motion` 与 `finalize-a09`。A08 在 1.0x/2.0x 各覆盖十个屏幕，
使用真实 TalkBack explore-by-touch 与 next-item 手势，并在每次手势后绑定完整
`dumpsys accessibility`；每个可见文本节点的所有字符框在同一次全屏背景排除
计算中参与。A09 普通动态固定 60 帧，减少动态固定 13 帧转换和两组 26 帧 idle，
最终视觉复审必须绑定普通与减少动态的精确 frame-set 摘要。

```sh
python3 -m t01m_host run-a07 --serial SERIAL --apk FINAL.apk \
  --observability-client /absolute/read-only-daemon-observer \
  --output /absolute/evidence/root
python3 -m t01m_host run-a08 --serial SERIAL --apk FINAL.apk \
  --output /absolute/evidence/root
python3 -m t01m_host capture-normal-motion --serial SERIAL --apk FINAL.apk \
  --output /absolute/evidence/root
python3 -m t01m_host run-reduced-motion --serial SERIAL --apk FINAL.apk \
  --output /absolute/evidence/root
python3 -m t01m_host finalize-a09 --root /absolute/evidence/root \
  --visual-review /absolute/evidence/root/artifacts/a09-visual-review.json
```

A07 全程离线完整重跑 A06，并验证 force-stop/冷启动持久化、横竖屏功能可达、
清数据恢复 onboarding、共享存储不变、目标 UID 的 tcp/tcp6/udp/udp6 socket、
netstats、目标 logcat 和 daemon 全局 ledger 不变。控制器先关闭网络并建立可观测
环境，再主动调用 daemon observer 的 before 采集；之后才 force-stop 目标、清空
logcat 游标并取得未启动目标的 UID/netstats/proc socket/共享存储基线。一个
100 ms UID socket sampler 在第一次冷启动前同步启动，覆盖 A06、后续冷启动、
旋转、清数据与最终状态采集，因此只在端点之间短暂存在的 socket 也会令 A07
失败。`run-as` 仅建立 app-private 文件路径、大小和摘要清单；私有文件内容不会
写入证据。

`--observability-client` 必须是显式给出的绝对路径、非符号链接、可执行普通文件。
控制器分别执行：

```text
CLIENT capture --phase before|after --run-nonce RUN \
  --capture-nonce CAPTURE --application-id dev.lilies.civilizationseed
```

客户端必须只向 stdout 写一行 canonical JSON，stderr 必须为空。对象字段集合固定
为 `schema_version`、`kind`、`phase`、`run_nonce`、`capture_nonce`、
`capture_id`、`captured_at_unix_ns`、`coverage_epoch`、`daemon`、`ledger`、
`attestation`。`coverage_epoch` 含稳定的 `id` 与 `started_at_unix_ns`；
`daemon` 含稳定的 `fingerprint_sha256` 与 `instance_id`；`ledger` 精确含
`model_calls`、`input_tokens`、`output_tokens`、`unknown_token_events`、
`tool_calls`、`cost_microunits` 六个非负整数。`attestation` 必须声明
`complete=true`、`read_only=true`，并以 `producer_sha256` 绑定客户端文件。
所有 schema version、计数器及时间戳都必须是 JSON integer；JSON boolean
即使在 Python 中可按整数比较也一律拒绝。
控制器生成并核对 run/capture nonce，绑定实际调用时间和不同 artifact path/raw
digest/capture ID，并要求 `before captured_at < workload start <= workload
complete < after captured_at`。同一 coverage epoch 与 daemon 实例上的全部
ledger 字段必须完全不变；静态文件、任意 JSON、相同快照或缺字段快照均不能通过。

宿主在安装前关闭飞行模式以外的所有网络通道并打开飞行模式，再安装冻结 driver
和目标 APK；A07 runner 随后重新验证离线状态并按上述次序取得未启动基线和两份
静止共享存储快照。每个原子语义动作都记录开始/结束时间、动作与 selector、
before/after 无障碍树 SHA-256、断言输入和观测值、匹配的脱敏
logcat；命名里程碑和失败另存 PNG。全局动作和 scoped 动作一律使用严格 UTF-8
Base64；坐标、全局序号或截图本身不能建立 PASS。

`device-control` 支持清数据、安装流程之外的强制停止/冷启动、横竖屏、
`font_scale` 1.0/2.0、三项动画比例 0/1，以及冻结真实 TalkBack 的启停和摘要
校验。`snapshot-shared-storage` 对 `/storage/emulated/0` 的每个可读普通文件记录
规范化相对路径、字节数和内容 SHA-256，读取中尺寸变化即失败。

## A08/A09 确定性数学

完整 A08 对比度路径与 A09 的 sRGB 预处理只通过
`numeric-reference/NumericReference` 执行。该适配器不是规范权威；权威是最终
`acceptance-oracle.json` 的 inline 算法与 `toolchain-lock.json` 已绑定的 JBR
21 Java strict floating point / `java.lang.StrictMath`。宿主在每次使用前校验
JBR `java`、JBR `release`、适配器源码和全部 class 摘要。

适配器对截图、原始字符框和全部 256 个 sRGB 输入建立二进制协议，在 JBR 内完成
字符框取整、三像素外环、冻结坐标映射、逐通道 OLS 加恰好十轮 Huber IRLS、
OKLab 距离、连通分量、actual-pixel core 与逐像素 WCAG 对比度。报告保留实际
使用的 256 项 q-to-raw-binary64 表、字符框输入 raw bits、背景系数及每个入选
像素的 P/B/距离/比例 raw bits；A08 不会调用 Python `math` 或宿主 libm。

A09 的 SVD、去趋势、相关、自相关、silhouette 统计等在
`t01m_host.math_oracle` / `t01m_host.measure` 中冻结；其第一奇异向量符号只由
最小的最大绝对 right-loading 索引决定，与 silhouette 结果无关。减少动态的每帧
记录 request sequence、`takeScreenshot()` 前的 capture-start、像素复制完成后的
capture-complete 和 immutable ARGB buffer SHA-256；宿主再从 PNG 独立重算该
buffer 摘要。PNG 解码器只接受不透明、8-bit、非隔行 RGB/RGBA 无损证据。
正常动态 lane 固定捕获 60 帧、目标间隔 200ms；hero ROI 外逐像素必须静止，
ROI 内执行背景拟合、OKLab silhouette、中心白发种子、边缘裁剪拒绝、
pixel/silhouette 周期与幅度以及 swept-character/attached-glow 相关归因。
机器报告仅达到 `ready_for_visual_review`；新鲜只读复核还必须逐帧确认闭眼、
角色契约与克制深紫风格。

## A10 清单与闭包

`build-leaves` 要求一个逐文件 producer receipt/case 覆盖索引，并对 `artifacts/`
下每个普通文件恰好收录一次。`build-manifest` 会强制检查冻结的机器工件和八张
截图，不允许 manifest 自指或引用 review/closure。新鲜只读复审完成后，
`build-closure` 只接受与原始 manifest 摘要精确绑定、A01–A09 全部 PASS、
A10 pre-review PASS、零 blocker、reviewer identity/context、fresh/read-only
independence 以及精确 remaining predicates 的 review。完整 DFS 会拒绝自边、
重复边、反向边、缺失 target 和任意环。final APK 必须是 evidence root 下的
规范化相对普通文件，A01–A09 每个 result leaf 还必须实际报告 `result=pass`。

纯 fixture 自测：

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q t01m_host tests
```

driver 可复现构建不保存私钥，要求 `T01M_SIGNING_KEY_PATH` 和
`T01M_SIGNING_CERT_PATH` 指向 lock 摘要绑定的外部文件。脚本自动执行两次独立
编译、D8、固定 ZIP 元数据、zipalign、v2-only 签名，并比较 APK 字节：

```sh
export T01M_SIGNING_KEY_PATH=/external/path/key.pk8
export T01M_SIGNING_CERT_PATH=/external/path/cert.pem
python3 scripts/rebuild_driver.py \
  --output /tmp/t01m-driver-a.apk
python3 scripts/rebuild_driver.py \
  --output /tmp/t01m-driver-b.apk \
  --verify-against /tmp/t01m-driver-a.apk
```

私钥和证书若位于仓库内会被脚本拒绝。`scripts/regenerate_lock.py` 只在 README、
测试、脚本与最终 APK 全部稳定后，机械更新 source、host、DEX、APK 与外部签名
摘要。driver APK 声明零 permission，最终仍须用锁定 `apkanalyzer` 和
`apksigner` 验证零权限与 v2-only。

当前 claim ceiling 只覆盖 oracle 源码/APK、完整执行路由、确定性算法、双重构和
fixture/static 验证。未显式提供并执行目标证据时，不声明任何目标依赖的
A01–A10 runtime PASS。

JBR 数值参考同样由锁定 `javac`/`release` 在两个独立临时目录重建，四个 class
必须逐字节一致后才原子发布：

```sh
python3 scripts/rebuild_numeric_reference.py
```
