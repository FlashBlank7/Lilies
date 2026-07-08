# 钉钉智能打卡 — 完整方案 (Lilies 生成)

## 文件清单

```
你需要传到手机上的:
├── dingtalk_smart_punch.sh   ← ⭐ 主脚本 (Termux 执行)
├── install.sh                ← 一键安装
│
├── dingtalk_fullauto.sh      ← 旧版 (保留)
├── all_in_one.sh             ← 旧版 (保留)
├── dingtalk_punch.sh         ← 旧版 (保留)
│
└── Lilies 模板 (服务器端):
    ├── ../templates/dingtalk_smart_punch.json  ← 智能打卡工作流
    ├── ../templates/dingtalk_checkin.json      ← 旧版 API 打卡
    └── ../templates/dingtalk_checkout.json     ← 旧版 API 签退
```

## 策略架构 (Tiered Fallback)

```
┌──────────────────────────────────────────────────────┐
│              钉钉智能打卡 v4.0                         │
│                                                      │
│  输入: 上班/下班 + 校准坐标                            │
│                                                      │
│  ┌──────────────────────┐                             │
│  │ Tier 1: 急速打卡      │                             │
│  │ 启动钉钉 App          │                             │
│  │ 利用急速打卡自动完成   │                             │
│  └──────┬───────────────┘                             │
│         │ 失败?                                        │
│         ▼                                              │
│  ┌──────────────────────┐                             │
│  │ Tier 2: 模拟点击      │                             │
│  │ 唤醒屏幕              │                             │
│  │ 启动钉钉              │                             │
│  │ input tap (X,Y) 点击  │                             │
│  │ 截图留证              │                             │
│  └──────┬───────────────┘                             │
│         │ 失败?                                        │
│         ▼                                              │
│  ┌──────────────────────┐                             │
│  │ Tier 3: 人工通知      │                             │
│  │ 打开钉钉              │                             │
│  │ 发送振动提醒          │                             │
│  └──────────────────────┘                             │
│                                                      │
│  输出: 打卡结果 + 截图 + 日志                          │
└──────────────────────────────────────────────────────┘
```

## 方案对比

| 方案 | 自动化程度 | 需要什么 | 推荐度 |
|------|-----------|---------|--------|
| **A: dingtalk_smart_punch.sh** | 全自动（三级降级） | Termux + Termux:API | ⭐⭐⭐⭐⭐ |
| B: dingtalk_fullauto.sh | 全自动（只有模拟点击） | Termux + Termux:API | ⭐⭐⭐⭐ |
| C: all_in_one.sh | 半自动（只开App） | Termux | ⭐⭐⭐ |
| D: HTML 页面 | 提醒模式 | 仅浏览器 | ⭐⭐ |

## 快速部署（5 分钟）

### 前置条件

1. Android 手机（无需 root）
2. 从 [F-Droid](https://f-droid.org) 安装 **Termux** 和 **Termux:API**
3. 启用「开发者选项 → 指针位置」（用于校准坐标）

### 步骤

```bash
# 1. Termux 中授予存储权限
termux-setup-storage
# (弹出权限 → 允许)

# 2. 复制文件到 Termux
cp ~/storage/downloads/dingtalk_smart_punch.sh ~/
cp ~/storage/downloads/install.sh ~/

# 3. 一键安装
bash ~/install.sh
```

### 手动部署

```bash
# 1. 校准坐标
bash ~/dingtalk_smart_punch.sh calibrate

# 2. 测试
bash ~/dingtalk_smart_punch.sh checkin-test

# 3. 部署
bash ~/dingtalk_smart_punch.sh install
```

## 日常维护

| 操作 | 命令 |
|------|------|
| 查看状态 | `bash ~/dingtalk_smart_punch.sh status` |
| 查看日志 | `tail -f ~/dingtalk_smart_punch.log` |
| 查看截图 | 手机相册 DCIM/dingtalk_punches/ |
| 重新校准 | `bash ~/dingtalk_smart_punch.sh calibrate` |
| 立即打卡 | `bash ~/dingtalk_smart_punch.sh checkin` |
| 暂停打卡 | `crontab -e` → 注释打卡行 |

## 常见问题

**Q: 钉钉更新后坐标变了怎么办？**
A: 重新运行 `bash ~/dingtalk_smart_punch.sh calibrate`

**Q: 手机重启后 cron 还在吗？**
A: 需要重新启动: 打开 Termux → `bash ~/dingtalk_smart_punch.sh status` → 如果 cron 未运行会自动提示

**Q: 屏幕锁了能自动打卡吗？**
A: 校准时可设置 `screen_lock_swipe=1` 启用上滑解锁。如有密码/指纹锁，需在 Smart Lock 中设置可信地点。

**Q: 为什么要三级降级？**
A: 钉钉「急速打卡」在 Wi-Fi/GPS 就绪时自动生效，但有时会失灵。此时 `input tap` 模拟点击可备用。最后一层是人工通知，确保不会漏打卡。

**Q: 这会触发钉钉风控吗？**
A: `input tap` 是 Android 系统级命令，模拟真实触摸事件，钉钉无法区分。截图留证确保可追溯。

## Lilies 集成

此脚本由 Lilies 智能体工作流平台设计生成。对应的服务端模板:

- `templates/dingtalk_smart_punch.json` — 完整决策工作流
- Lilies 可定时触发、监控状态、发送 SSE 事件
