# 钉钉智能打卡 v4.0 — 完整部署方案

> Lilies 智能体工作流平台生成 | 2026-07-06

---

## 一、你需要准备

| 项目 | 说明 |
|------|------|
| Android 手机 | 无需 root |
| F-Droid | https://f-droid.org 下载 |
| Termux | F-Droid 内安装 |
| Termux:API | F-Droid 内安装（可选，用于通知） |

---

## 二、传到手机的文件

```
📱 放到手机 Downloads 目录:
├── dingtalk_smart_punch.sh   ← 核心脚本
├── install.sh                ← 一键安装
└── cleanup_old.sh            ← 旧版清理
```

传文件方式：微信/QQ 发给自己 → 保存到 Downloads。

---

## 三、部署（两种情况）

### 情况 A：首次部署（新手机，没用过旧版）

```bash
# 1. 设置存储权限
termux-setup-storage
# 弹出权限 → 点「允许」

# 2. 复制文件
cp ~/storage/downloads/dingtalk_smart_punch.sh ~/
cp ~/storage/downloads/install.sh ~/

# 3. 一键安装
bash ~/install.sh
```

安装向导会引导：
- 校准打卡按钮坐标
- 设置考勤入口坐标（默认 888, 388）
- 配置 ADB 模式（Android 12+ 默认启用）
- 安装 cronie 并配置定时任务

### 情况 B：从旧版升级

```bash
# 1. 清理旧残留（预览）
bash ~/cleanup_old.sh

# 2. 确认后执行清理
bash ~/cleanup_old.sh --exec

# 3. 覆盖新版脚本
cp ~/storage/downloads/dingtalk_smart_punch.sh ~/

# 4. 重新校准 + 部署
bash ~/dingtalk_smart_punch.sh install
```

---

## 四、ADB 无线调试配置（Android 12+ 必须）

如果你看到 `SecurityException: INJECT_EVENTS` 错误，需要配置这一步。

### 4.1 开启无线调试

```
设置 → 开发者选项 → 无线调试 → 开启
```

> 如果找不到「开发者选项」：设置 → 关于手机 → 连点 7 次「版本号」

### 4.2 配对

```
无线调试页面 → 点击「使用配对码配对设备」
→ 弹窗显示：配对码 123456 | IP:Port 192.168.x.x:4xxxx

Termux 中执行:
$ pkg install android-tools
$ adb pair 192.168.x.x:4xxxx
Enter pairing code: 123456
→ Successfully paired
```

### 4.3 连接

```
无线调试主页面查看端口号（如 4yyyy）

Termux:
$ adb connect 127.0.0.1:4yyyy
→ connected to 127.0.0.1:4yyyy
```

> ⚠️ 注意：配对端口和连接端口不是同一个。配对口在弹窗里，连接端口在主页面。

### 4.4 验证

```bash
adb shell input tap 540 1200
```

没报 SecurityException 即成功。

---

## 五、验证部署

```bash
# 查看整体状态
bash ~/dingtalk_smart_punch.sh status

# 预期输出:
#   ✅ 配置文件
#   ✅ 定时任务已配置 (8:45/19:00)
#   ✅ cron 服务正在运行
#   ✅ input tap 权限正常
```

### 测试打卡

```bash
# 上班打卡测试
bash ~/dingtalk_smart_punch.sh checkin-test
# 输入 y 确认

# 下班打卡测试
bash ~/dingtalk_smart_punch.sh checkout-test
```

---

## 六、日常命令速查

| 操作 | 命令 |
|------|------|
| 查看状态 | `bash ~/dingtalk_smart_punch.sh status` |
| 查看日志 | `tail -f ~/dingtalk_smart_punch.log` |
| 重新校准 | `bash ~/dingtalk_smart_punch.sh calibrate` |
| 重启后重连 ADB | `bash ~/dingtalk_smart_punch.sh adb-reconnect` |
| 立即上班打卡 | `bash ~/dingtalk_smart_punch.sh checkin` |
| 立即下班打卡 | `bash ~/dingtalk_smart_punch.sh checkout` |
| 暂停服务 | `crontab -e` → 注释打卡行 |
| 恢复服务 | `bash ~/dingtalk_smart_punch.sh install` |

---

## 七、定时任务

```
周一至周五:
  08:45 → 上班打卡
  19:00 → 下班签退

策略（自动降级）:
  Tier 1: 急速打卡（启动钉钉自动触发）
  Tier 2: 模拟点击（input tap 坐标点击）
  Tier 3: 通知提醒（手动完成）
```

---

## 八、常见问题

**Q: 重启后还能自动打卡吗？**
A: 脚本每次打卡前会自动尝试重连 ADB。如果自动重连失败，手动跑一次：
```bash
bash ~/dingtalk_smart_punch.sh adb-reconnect
```

**Q: 钉钉更新后坐标变了？**
A: 重新校准：
```bash
bash ~/dingtalk_smart_punch.sh calibrate
```

**Q: 如何查看打卡是否成功？**
A: 截图在 `DCIM/dingtalk_punches/`，日志在 `~/dingtalk_smart_punch.log`

**Q: 不需要 ADB 的方案有吗？**
A: 手机系统 < Android 12 或已 root 的设备不需要 ADB。2019 年之前的旧手机通常直接支持 `input tap`。
