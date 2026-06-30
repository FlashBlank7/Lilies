# 钉钉全自动打卡 — 手机端部署完整流程

> 全程约 10 分钟，之后完全自动运行，无需任何手动操作。

---

## 第一步：安装 Termux（3 分钟）

```
⚠ 重要: 必须从 F-Droid 下载，Google Play 版本已过时

1. 手机浏览器打开: https://f-droid.org
2. 下载 F-Droid APK → 安装
3. 打开 F-Droid → 搜索 "Termux" → 安装
4. 搜索 "Termux:API" → 安装
```

安装后桌面上有两个图标：`Termux` 和 `Termux:API`

---

## 第二步：传文件到手机（1 分钟）

把这三个文件传到手机任意目录（推荐 Downloads）：

```
你需要传到手机的:
├── dingtalk_fullauto.sh    ← 核心脚本
└── install.sh              ← 一键安装
```

**传文件方式**（任选一种）：
- 微信/QQ 发给自己 → 手机端保存到 Downloads
- USB 数据线 → 复制到手机 Downloads 文件夹
- 蓝牙/AirDrop

---

## 第三步：复制文件到 Termux（2 分钟）

打开 Termux，执行：

```bash
# 设置存储权限
termux-setup-storage
# 弹出权限请求 → 点"允许"

# 复制文件到 Termux 目录
cp ~/storage/downloads/dingtalk_fullauto.sh ~/
cp ~/storage/downloads/install.sh ~/

# 如果文件名不对，先看一下实际文件名
ls ~/storage/downloads/ | grep dingtalk
```

---

## 第四步：一键安装（1 分钟）

```bash
cd ~
bash install.sh
```

这会自动：
- 安装 cronie（定时任务）
- 配置 crontab（8:45 上班 + 19:00 下班，周一至周五）
- 启动 cron 服务

安装完成后显示：
```
━━━━━━━━━━━━━━━━━━━━━
  安装完成! ✅
━━━━━━━━━━━━━━━━━━━━━
```

---

## 第五步：校准坐标（2 分钟）

```bash
./dingtalk_fullauto.sh calibrate
```

校准流程：

```
1. 先打开「开发者选项 → 指针位置」（显示触摸坐标）
   (设置 → 关于手机 → 连点 7 次"版本号" → 开发者选项 → 指针位置)

2. 手动操作一遍打卡流程：
   打开钉钉 → 点「工作」→ 点「考勤打卡」→ 记住每个点击位置的坐标

3. 按提示输入坐标值：
   考勤入口 X 坐标: 540   ← 屏幕中间
   考勤入口 Y 坐标: 1800  ← 底部 tab 位置
   上班打卡按钮 X: 540
   上班打卡按钮 Y: 1200
   下班打卡按钮 X: 540
   下班打卡按钮 Y: 1200   ← 通常和上班同一位置
```

---

## 第六步：测试

```bash
# 手动触发一次上班打卡测试
./dingtalk_fullauto.sh checkin
```

你会看到：
- 屏幕被唤醒
- 钉钉自动打开
- 自动点击打卡按钮
- 通知提示"已完成"

如果坐标不准，重新运行 `calibrate` 调整。

---

## 完成！

```
✅ 之后每天:
   08:45 → 自动唤醒屏幕 → 打开钉钉 → 点击上班打卡 → 截图 → 回桌面
   19:00 → 自动唤醒屏幕 → 打开钉钉 → 点击下班打卡 → 截图 → 回桌面
```

### 日常维护

| 操作 | 命令 |
|------|------|
| 查看打卡截图 | 手机相册 DCIM 目录 |
| 查看运行日志 | Termux 内执行 `tail ~/dingtalk_punch.log` |
| 查看定时任务 | `crontab -l` |
| 暂停打卡 | `crontab -e` → 注释掉两行 |
| 重新校准坐标 | `./dingtalk_fullauto.sh calibrate` |

### 常见问题

**Q: 钉钉更新后坐标变了怎么办？**
A: 重新运行 `./dingtalk_fullauto.sh calibrate`

**Q: 手机重启后 cron 还在吗？**
A: 每次重启后需要手动启动: 打开 Termux → `crond` → 关掉 Termux（cron 在后台运行）

**Q: 屏幕锁了能自动打卡吗？**
A: 脚本会先用 `input keyevent 26` 唤醒屏幕，然后上滑解锁。如果设置了密码/指纹锁，可能需要在 Android 的 Smart Lock 中把"可信地点（家/公司）"设为自动解锁。

**Q: 周末也打卡了怎么办？**
A: crontab 中 `1-5` 表示周一至周五，周末不会触发。如需周末也打卡，改 crontab 为 `*`：
```bash
crontab -e
# 把 1-5 改成 *
```
