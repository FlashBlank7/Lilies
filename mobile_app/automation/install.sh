#!/data/data/com.termux/files/usr/bin/bash
# ═══════════════════════════════════════════════════════════
#  一键安装 — 钉钉智能打卡 v4.0 (Lilies 生成)
#
#  在 Termux 中执行:
#   bash install.sh
# ═══════════════════════════════════════════════════════════

set -e

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  钉钉智能打卡 v4.0 — 一键安装"
echo "  Lilies 工作流平台"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SMART_PUNCH="$SCRIPT_DIR/dingtalk_smart_punch.sh"

if [ ! -f "$SMART_PUNCH" ]; then
    echo "❌ 找不到 dingtalk_smart_punch.sh"
    echo "   请确保 install.sh 和 dingtalk_smart_punch.sh 在同一个目录"
    exit 1
fi

# 复制到 HOME
cp "$SMART_PUNCH" "$HOME/dingtalk_smart_punch.sh"
chmod +x "$HOME/dingtalk_smart_punch.sh"

# 自动校准（交互式）
echo ""
echo "━ 步骤 1: 坐标校准 ━"
echo ""
echo "⚠️  首次使用需要校准打卡按钮的屏幕坐标。"
echo "如果你已经知道坐标，可以在校准中直接输入。"
echo ""
read -p "现在开始校准? [Y/n]: " do_cal
if [ "$do_cal" != "n" ] && [ "$do_cal" != "N" ]; then
    bash "$HOME/dingtalk_smart_punch.sh" calibrate
else
    echo "跳过校准（之后可手动运行: bash ~/dingtalk_smart_punch.sh calibrate）"
fi

# 一键部署
echo ""
echo "━ 步骤 2: 部署定时任务 ━"
bash "$HOME/dingtalk_smart_punch.sh" install

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✅ 全部完成!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
