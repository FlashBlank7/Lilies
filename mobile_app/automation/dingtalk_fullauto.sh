#!/data/data/com.termux/files/usr/bin/bash
# ═══════════════════════════════════════════════════════════
#  钉钉全自动打卡 — 无人值守版
#
#  依赖: Termux + Termux:API (都从 F-Droid 免费安装)
#  原理: 定时 → 打开钉钉 → 等待加载 → 模拟点击打卡
#
#  安装:
#    pkg install termux-api cronie
#    chmod +x dingtalk_fullauto.sh
#    crontab -e
#      45 8 * * 1-5 ~/dingtalk_fullauto.sh checkin
#       0 19 * * 1-5 ~/dingtalk_fullauto.sh checkout
#
#  校准 (首次使用):
#    ./dingtalk_fullauto.sh calibrate
#    → 按提示点击屏幕 → 自动记录坐标
# ═══════════════════════════════════════════════════════════

CONFIG_FILE="$HOME/.dingtalk_coords"
MODE="${1:-checkin}"

# ── 校准模式: 记录打卡按钮的屏幕坐标 ──
calibrate() {
  echo "━━━ 校准模式 ━━━"
  echo ""
  echo "现在请手动操作一遍打卡流程，我会记录你的点击坐标。"
  echo ""

  # 1. 打开钉钉
  echo "📲 正在打开钉钉..."
  am start -n com.alibaba.android.rimet/.LaunchActivity 2>/dev/null
  sleep 3

  # 2. 记录考勤入口点击
  termux-notification --title "校准步骤 1/3" \
    --content "请点击钉钉底部「工作」或「考勤打卡」入口，然后等3秒" \
    --priority high
  echo "📍 请点击考勤入口..."
  sleep 8
  read -p "按回车继续..."

  # 3. 记录打卡按钮点击
  termux-notification --title "校准步骤 2/3" \
    --content "请点击「上班打卡」或「下班打卡」按钮，然后等3秒" \
    --priority high
  echo "📍 请点击打卡按钮..."
  sleep 8
  read -p "按回车继续..."

  # 4. 用 getevent 或手动输入坐标
  echo ""
  echo "请手动输入以下坐标 (可通过「开发者选项 → 指针位置」查看):"
  read -p "考勤入口 X 坐标: " entry_x
  read -p "考勤入口 Y 坐标: " entry_y
  read -p "上班打卡按钮 X 坐标: " checkin_x
  read -p "上班打卡按钮 Y 坐标: " checkin_y
  read -p "下班打卡按钮 X 坐标: " checkout_x
  read -p "下班打卡按钮 Y 坐标: " checkout_y

  cat > "$CONFIG_FILE" << EOF
entry_x=$entry_x
entry_y=$entry_y
checkin_x=$checkin_x
checkin_y=$checkin_y
checkout_x=$checkout_x
checkout_y=$checkout_y
EOF

  echo ""
  echo "✅ 校准完成! 坐标已保存到 $CONFIG_FILE"
  echo "现在可以测试: ./dingtalk_fullauto.sh checkin"
}

# ── 加载坐标配置 ──
load_config() {
  if [ ! -f "$CONFIG_FILE" ]; then
    termux-notification --title "钉钉打卡错误" \
      --content "请先运行 calibrate 校准坐标" --priority high
    exit 1
  fi
  source "$CONFIG_FILE"
}

# ── 执行打卡 ──
do_punch() {
  local type="$1"
  load_config

  local label tap_x tap_y
  if [ "$type" == "checkin" ]; then
    label="☀️ 上班打卡 08:45"
    tap_x="$checkin_x"; tap_y="$checkin_y"
  else
    label="🌙 下班签退 19:00"
    tap_x="$checkout_x"; tap_y="$checkout_y"
  fi

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $label 开始..."

  # 1. 唤醒屏幕
  input keyevent 26  # Power 键唤醒
  sleep 1
  input swipe 500 1500 500 500  # 上滑解锁 (大部分手机)
  sleep 1

  # 2. 打开钉钉
  am start -n com.alibaba.android.rimet/.LaunchActivity 2>/dev/null || \
  am start -a android.intent.action.VIEW -d "dingtalk://" 2>/dev/null
  echo "  📲 钉钉启动中..."
  sleep 5  # 等钉钉完全加载

  # 3. 点击「工作」/「考勤打卡」入口
  if [ -n "$entry_x" ] && [ -n "$entry_y" ]; then
    input tap "$entry_x" "$entry_y"
    echo "  👆 点击考勤入口 ($entry_x, $entry_y)"
    sleep 3
  fi

  # 4. 点击打卡按钮
  input tap "$tap_x" "$tap_y"
  echo "  👆 点击打卡按钮 ($tap_x, $tap_y)"
  sleep 2

  # 5. 截图留证
  screencap -p "$HOME/storage/shared/dcim/punch_${type}_$(date +%Y%m%d_%H%M%S).png" 2>/dev/null

  # 6. 通知结果
  termux-notification \
    --title "钉钉$label" \
    --content "✅ 已完成 — $(date '+%H:%M:%S')" \
    --priority high \
    --vibrate 300,100,300

  # 7. 回到桌面
  sleep 2
  input keyevent 3  # Home

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $label 完成 ✅"
}

# ── 主入口 ──
case "$MODE" in
  calibrate) calibrate ;;
  checkin)   do_punch "checkin" ;;
  checkout)  do_punch "checkout" ;;
  *)
    echo "用法: $0 [calibrate|checkin|checkout]"
    echo ""
    echo "  首次使用: $0 calibrate   (记录屏幕坐标)"
    echo "  上班打卡: $0 checkin"
    echo "  下班签退: $0 checkout"
    ;;
esac
