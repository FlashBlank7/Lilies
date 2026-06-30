#!/data/data/com.termux/files/usr/bin/bash
# ═══════════════════════════════════════════════════════
#  一键安装 — 钉钉全自动打卡
#
#  在 Termux 中执行:
#   bash install.sh
# ═══════════════════════════════════════════════════════

set -e

echo "━━━━━━━━━━━━━━━━━━━━━"
echo "  钉钉全自动打卡 - 安装"
echo "━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 1. 安装依赖
echo "📦 安装依赖..."
pkg update -q
pkg install -y termux-api cronie

# 2. 复制脚本
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cp "$SCRIPT_DIR/dingtalk_fullauto.sh" "$HOME/dingtalk_fullauto.sh"
chmod +x "$HOME/dingtalk_fullauto.sh"

# 3. 配置 crontab (周一至周五)
echo "⏰ 配置定时任务..."
CRON_TMP=$(mktemp)
crontab -l 2>/dev/null > "$CRON_TMP" || true
grep -q "dingtalk_fullauto" "$CRON_TMP" || {
  echo "# 钉钉自动打卡 — 周一至周五" >> "$CRON_TMP"
  echo "45 8 * * 1-5 $HOME/dingtalk_fullauto.sh checkin" >> "$CRON_TMP"
  echo "0 19 * * 1-5 $HOME/dingtalk_fullauto.sh checkout" >> "$CRON_TMP"
}
crontab "$CRON_TMP"
rm "$CRON_TMP"

# 4. 启动 cron 服务
echo "🚀 启动 cron 服务..."
sv-enable crond 2>/dev/null || echo "  (手动启动: crond)"

# 5. 校准
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━"
echo "  安装完成! ✅"
echo "━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "下一步: 校准坐标"
echo "  $HOME/dingtalk_fullauto.sh calibrate"
echo ""
echo "测试打卡:"
echo "  $HOME/dingtalk_fullauto.sh checkin"
echo ""
echo "查看 crontab:"
echo "  crontab -l"
echo ""
