# 钉钉自动打卡 — 完整方案

## 文件清单

```
你需要带到手机上的:
├── dingtalk_punch.html           ← ⭐ 主文件 (手机浏览器打开即用)
│
├── automation/
│   ├── DingTalk_Auto_Punch.prf.xml  ← Tasker 配置 (推荐, 需安装 Tasker)
│   ├── dingtalk_punch.sh            ← Termux 脚本 (需安装 Termux)
│   └── README.md                    ← 本文件
│
└── templates/
    ├── dingtalk_checkin.json        ← Lilies 工作流 (服务器端可选)
    └── dingtalk_checkout.json
```

## 方案对比

| 方案 | 自动化程度 | 需要什么 | 推荐度 |
|------|-----------|---------|--------|
| **A: HTML 页面** | 半自动 (到时间弹出提醒, 需手动点一下) | 仅浏览器 | ⭐⭐⭐⭐⭐ |
| **B: Tasker** | 全自动 (定时打开钉钉App) | Tasker App (¥22) | ⭐⭐⭐⭐ |
| **C: Termux** | 全自动 (cron定时+通知) | Termux + Termux:API | ⭐⭐⭐ |

## 方案 A: HTML 页面 (最简单, 零配置)

1. 把 `dingtalk_punch.html` 传到手机
2. Chrome 打开
3. 保持标签页打开
4. 到 8:45/19:00 会自动弹出提醒 + 打开钉钉 App
5. 进钉钉后手动点"考勤打卡"

> 即使不配 Token 也能用 — 到时间会自动跳转钉钉

## 方案 B: Tasker (最可靠)

1. 安装 Tasker (Google Play)
2. Tasker → 长按"配置文件" → 导入
3. 选择 `DingTalk_Auto_Punch.prf.xml`
4. 授予通知权限和辅助功能权限
5. 完成 — 每天 8:45 和 19:00 自动打开钉钉

> 可选: 配合 AutoInput 插件实现完全自动点击打卡按钮

## 方案 C: Termux (免费, 命令行)

```bash
# 1. F-Droid 安装 Termux + Termux:API
# 2. 安装依赖
pkg install termux-api cronie

# 3. 复制脚本
cp dingtalk_punch.sh ~/
chmod +x ~/dingtalk_punch.sh

# 4. 启动 cron
sv-enable crond

# 5. 编辑定时任务
crontab -e
```

```
# 添加这两行:
45 8 * * 1-5 ~/dingtalk_punch.sh checkin
0 19 * * 1-5 ~/dingtalk_punch.sh checkout
```

## 原理

```
┌─────────────────────────────────┐
│  你的手机                        │
│                                 │
│  8:44:55  浏览器时钟 tick        │
│  8:45:00  触发 → 打开钉钉App     │
│  8:45:05  你点"考勤打卡" ✅       │
│                                 │
│  18:59:55 浏览器时钟 tick        │
│  19:00:00 触发 → 打开钉钉App     │
│  19:00:05 你点"考勤打卡" ✅       │
└─────────────────────────────────┘
```

## 为什么不能完全自动点"打卡按钮"?

钉钉的考勤打卡:
1. 需要真实 GPS 定位（不能 mock）
2. 需要在钉钉 App 内部点击
3. 有时需要蓝牙/WiFi 辅助定位
4. 企业管理员可以看到打卡来源（App vs API）

所以最可靠的方式是: **自动提醒 + 手动点击**。这既合规又不容易被风控。
