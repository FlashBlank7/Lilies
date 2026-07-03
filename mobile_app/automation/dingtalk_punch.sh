#!/data/data/com.termux/files/usr/bin/bash
# ═══════════════════════════════════════════════════════
#  钉钉打卡 Termux 自动化脚本
#
#  安装 (Termux 内执行):
#    pkg install termux-api cronie
#    sv-enable crond
#    crontab -e
#
#  Crontab 配置:
#    45 8 * * 1-5 /data/data/com.termux/files/home/dingtalk_punch.sh checkin
#     0 19 * * 1-5 /data/data/com.termux/files/home/dingtalk_punch.sh checkout
#
#  依赖: Termux + Termux:API (F-Droid 安装)
# ═══════════════════════════════════════════════════════

MODE="${1:-checkin}"

send_notification() {
  local title="$1" msg="$2"
  termux-notification \
    --title "$title" \
    --content "$msg" \
    --priority high \
    --vibrate 500,200,500 \
    --action "android.intent.action.VIEW||dingtalk://"
}

if [ "$MODE" == "checkin" ]; then
  LABEL="☀️ 上班打卡"
  TIME="08:45"
elif [ "$MODE" == "checkout" ]; then
  LABEL="🌙 下班签退"
  TIME="19:00"
else
  echo "用法: $0 [checkin|checkout]"
  exit 1
fi

echo "[$(date '+%H:%M:%S')] $LABEL 触发..."

# 1. 发送通知
send_notification \
  "钉钉$LABEL" \
  "⏰ $TIME — 正在打开钉钉..."

# 2. 打开钉钉 App (通过 Android Intent)
am start -n com.alibaba.android.rimet/.LaunchActivity 2>/dev/null || \
am start -a android.intent.action.VIEW -d "dingtalk://" 2>/dev/null || \
termux-open-url "dingtalk://"

# 3. 等待 App 启动
sleep 4

# 4. 再次提醒（手动点击打卡按钮）
send_notification \
  "钉钉$LABEL" \
  "请点击考勤打卡按钮完成 $LABEL"

echo "[$(date '+%H:%M:%S')] 完成"
