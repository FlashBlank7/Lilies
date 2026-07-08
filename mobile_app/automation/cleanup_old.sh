#!/data/data/com.termux/files/usr/bin/bash
# ═══════════════════════════════════════════════════════════════
#  钉钉打卡 — 旧版本残留一键清理
#
#  用法:
#    bash cleanup_old.sh          # 仅检查
#    bash cleanup_old.sh --exec   # 执行清理
# ═══════════════════════════════════════════════════════════════

DRY_RUN=true
if [ "${1:-}" = "--exec" ]; then
    DRY_RUN=false
fi

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'

echo ""
echo -e "${BOLD}╔══════════════════════════════════════╗${NC}"
echo -e "${BOLD}║  钉钉打卡 — 旧版本残留清理          ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════╝${NC}"

if $DRY_RUN; then
    echo ""
    echo -e "  ${CYAN}ℹ️  预览模式 — 不会实际删除${NC}"
    echo -e "  ${CYAN}   确认无误后执行: bash $0 --exec${NC}"
fi
echo ""

cleaned=0

# ── 1. crontab 清理 ──────────────────────────────────────────

echo -e "${BOLD}1. 检查 crontab 残留${NC}"

CRON_BEFORE=$(crontab -l 2>/dev/null || echo "")

# 旧版本可能的关键词
OLD_PATTERNS="dingtalk|ding_punch|all_in_one|punch\.sh|fullauto|DingTalk"

if echo "$CRON_BEFORE" | grep -qE "$OLD_PATTERNS" 2>/dev/null; then
    echo -e "  ${YELLOW}发现旧 crontab 条目:${NC}"
    echo "$CRON_BEFORE" | grep -nE "$OLD_PATTERNS" | while read line; do
        echo -e "    ${RED}→${NC} $line"
    done

    if ! $DRY_RUN; then
        NEW_CRON=$(echo "$CRON_BEFORE" | grep -vE "$OLD_PATTERNS")
        if [ -z "$NEW_CRON" ]; then
            # crontab 完全为空，直接删除
            crontab -r 2>/dev/null || true
        else
            echo "$NEW_CRON" | crontab -
        fi
        echo -e "  ${GREEN}✅ 已清理 crontab${NC}"
    fi
    cleaned=$((cleaned + 1))
else
    echo -e "  ${GREEN}✅ crontab 无残留${NC}"
fi

# ── 2. 旧配置文件 ────────────────────────────────────────────

echo ""
echo -e "${BOLD}2. 检查旧配置文件${NC}"

OLD_CONFIGS=(
    "$HOME/.dingtalk_config"
    "$HOME/.dingtalk_coords"
)

for f in "${OLD_CONFIGS[@]}"; do
    if [ -f "$f" ]; then
        echo -e "  ${YELLOW}发现: $f${NC}"
        if ! $DRY_RUN; then
            rm -f "$f"
            echo -e "  ${GREEN}✅ 已删除: $f${NC}"
        fi
        cleaned=$((cleaned + 1))
    else
        echo -e "  ${GREEN}✅ 不存在: $(basename "$f")${NC}"
    fi
done

# ── 3. 旧脚本文件 ────────────────────────────────────────────

echo ""
echo -e "${BOLD}3. 检查旧脚本文件${NC}"

OLD_SCRIPTS=(
    "$HOME/dingtalk_allinone.sh"
    "$HOME/dingtalk_punch.sh"
    "$HOME/dingtalk_fullauto.sh"
    "$HOME/all_in_one.sh"
    "$HOME/dingtalk_punch.sh"
)

for f in "${OLD_SCRIPTS[@]}"; do
    if [ -f "$f" ]; then
        echo -e "  ${YELLOW}发现: $f${NC}"
        if ! $DRY_RUN; then
            rm -f "$f"
            echo -e "  ${GREEN}✅ 已删除: $f${NC}"
        fi
        cleaned=$((cleaned + 1))
    else
        echo -e "  ${GREEN}✅ 不存在: $(basename "$f")${NC}"
    fi
done

# ── 4. 旧日志文件 ────────────────────────────────────────────

echo ""
echo -e "${BOLD}4. 检查旧日志文件${NC}"

OLD_LOGS=(
    "$HOME/dingtalk_punch.log"
)

for f in "${OLD_LOGS[@]}"; do
    if [ -f "$f" ]; then
        size=$(wc -c < "$f" 2>/dev/null || echo 0)
        echo -e "  ${YELLOW}发现: $f (${size} bytes)${NC}"
        if ! $DRY_RUN; then
            rm -f "$f"
            echo -e "  ${GREEN}✅ 已删除: $f${NC}"
        fi
        cleaned=$((cleaned + 1))
    else
        echo -e "  ${GREEN}✅ 不存在: $(basename "$f")${NC}"
    fi
done

# ── 5. 旧 Tasker XML 残留 ────────────────────────────────────

echo ""
echo -e "${BOLD}5. 检查 Tasker 配置残留${NC}"

OLD_TASKER=(
    "$HOME/storage/downloads/DingTalk_Auto_Punch.prf.xml"
    "$HOME/DingTalk_Auto_Punch.prf.xml"
)

for f in "${OLD_TASKER[@]}"; do
    if [ -f "$f" ]; then
        echo -e "  ${YELLOW}发现: $f${NC}"
        if ! $DRY_RUN; then
            rm -f "$f"
            echo -e "  ${GREEN}✅ 已删除: $f${NC}"
        fi
        cleaned=$((cleaned + 1))
    else
        echo -e "  ${GREEN}✅ 不存在: $(basename "$f")${NC}"
    fi
done

# ── 6. 旧 crontab 备份文件 ───────────────────────────────────

echo ""
echo -e "${BOLD}6. 检查 crontab 临时文件${NC}"

OLD_TMP=(
    "$HOME/crontab_tmp"
)

for f in "${OLD_TMP[@]}"; do
    if [ -f "$f" ]; then
        echo -e "  ${YELLOW}发现: $f${NC}"
        if ! $DRY_RUN; then
            rm -f "$f"
            echo -e "  ${GREEN}✅ 已删除: $f${NC}"
        fi
        cleaned=$((cleaned + 1))
    else
        echo -e "  ${GREEN}✅ 不存在: $(basename "$f")${NC}"
    fi
done

# ── 汇总 ─────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}╔══════════════════════════════════════╗${NC}"
if $DRY_RUN; then
    if [ "$cleaned" -gt 0 ]; then
        echo -e "${BOLD}║  ${YELLOW}发现 $cleaned 项残留 (预览模式)${NC}       ${BOLD}║${NC}"
        echo -e "${BOLD}║  执行清理: bash $0 --exec  ${NC}${BOLD}║${NC}"
    else
        echo -e "${BOLD}║  ${GREEN}✅ 无残留，无需清理${NC}              ${BOLD}║${NC}"
    fi
else
    echo -e "${BOLD}║  ${GREEN}✅ 已清理 $cleaned 项残留${NC}              ${BOLD}║${NC}"
fi
echo -e "${BOLD}╚══════════════════════════════════════╝${NC}"

if ! $DRY_RUN && [ "$cleaned" -gt 0 ]; then
    echo ""
    echo -e "现在可以安装新版:"
    echo -e "  ${CYAN}bash ~/dingtalk_smart_punch.sh install${NC}"
fi

echo ""
