#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
#  all_in_one.sh - DingTalk Auto Check-in for Android
#  配合钉钉「急速打卡」使用 — 只需启动钉钉即可
#
#  部署: bash all_in_one.sh → 选 6 一键安装
# ============================================================
set -e

CONF="$HOME/.dingtalk_config"
LOG="$HOME/dingtalk_punch.log"
PKG="com.alibaba.android.rimet"

GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'
RED='\033[0;31m'; NC='\033[0m'; BOLD='\033[1m'

log() { echo "[$(date '+%H:%M:%S')] $1" | tee -a "$LOG"; }
ok()  { echo -e "${GREEN}OK${NC} $1"; }
step(){ echo -e "\n${BLUE}${BOLD}Step $1:${NC} $2"; }

banner(){
    clear
    echo -e "${BLUE}${BOLD}"
    echo "  ╔══════════════════════════════════╗"
    echo "  ║   DingTalk Auto Punch v3.0       ║"
    echo "  ║   配合急速打卡 | 一键部署         ║"
    echo "  ╚══════════════════════════════════╝"
    echo -e "${NC}"
}

# ---- Install cronie ----
install_deps(){
    step 1 "Installing cronie (task scheduler)"
    pkg update -q 2>/dev/null
    if ! dpkg -s cronie >/dev/null 2>&1; then
        pkg install -y cronie 2>/dev/null
        ok "cronie installed"
    else
        ok "cronie already installed"
    fi
}

# ---- Core: launch DingTalk ----
launch_dingtalk(){
    local label="$1"
    log "$label - launching DingTalk"

    # Wake screen
    input keyevent 26 2>/dev/null || true
    sleep 1

    # Open DingTalk
    am start -n "${PKG}/.biz.LaunchHomeActivity" 2>/dev/null || \
    am start -n "${PKG}/.LaunchActivity" 2>/dev/null || \
    am start -a android.intent.action.VIEW -d "dingtalk://" 2>/dev/null

    log "$label - DingTalk launched"
    sleep 8  # let quick punch complete

    # Kill DingTalk to save battery
    am force-stop "$PKG" 2>/dev/null || true
    log "$label - DingTalk closed"
}

# ---- Setup crontab ----
setup_cron(){
    step 2 "Setting up crontab (Mon-Fri: 8:45 + 19:00)"

    SCRIPT_PATH="$HOME/dingtalk_allinone.sh"
    cp "$0" "$SCRIPT_PATH" 2>/dev/null || true

    local tmpfile="$HOME/crontab_tmp"
    crontab -l 2>/dev/null > "$tmpfile" || true

    if ! grep -q "dingtalk" "$tmpfile" 2>/dev/null; then
        echo "# DingTalk auto punch (急速打卡: 只需启动钉钉)" >> "$tmpfile"
        echo "45 8 * * 1-5 bash $SCRIPT_PATH checkin  >> $LOG 2>&1" >> "$tmpfile"
        echo "0 19 * * 1-5 bash $SCRIPT_PATH checkout >> $LOG 2>&1" >> "$tmpfile"
        crontab "$tmpfile"
        ok "Crontab installed"
    else
        ok "Crontab already configured"
    fi
    rm -f "$tmpfile"

    echo ""
    echo -e "  ${YELLOW}Schedule:${NC}"
    crontab -l 2>/dev/null | grep dingtalk || echo "  (empty)"
}

# ---- Start cron (use runit so it survives terminal close) ----
start_cron(){
    step 3 "Starting cron service (survives terminal close)"

    # Ensure termux-services is installed
    if [ ! -d "$PREFIX/var/service" ]; then
        echo "  Installing termux-services..."
        pkg install -y termux-services 2>/dev/null
    fi

    # Create crond service directory (idempotent)
    local svc_dir="$PREFIX/var/service/crond"
    if [ ! -f "$svc_dir/run" ]; then
        mkdir -p "$svc_dir/log" 2>/dev/null
        cat > "$svc_dir/run" << 'RUNEOF'
#!/data/data/com.termux/files/usr/bin/bash
exec crond -f 2>&1
RUNEOF
        chmod 755 "$svc_dir/run"
    fi

    # Start via runit
    sv up crond 2>/dev/null || sv-enable crond 2>/dev/null || true
    sleep 1

    if sv status crond 2>/dev/null | grep -q 'run'; then
        ok "crond started (runit — survives terminal close)"
    else
        echo -e "  ${YELLOW}runit failed, using nohup fallback${NC}"
        nohup crond > /dev/null 2>&1 &
        disown
        ok "crond started (nohup)"
    fi
}

# ---- Test ----
test_launch(){
    step 4 "Testing: launching DingTalk now"
    launch_dingtalk "TEST"
    ok "Done. Check if DingTalk opened."
}

# ---- Main menu ----
main_menu(){
    banner
    echo "  1) Install cronie"
    echo "  2) Setup crontab (8:45 + 19:00, Mon-Fri)"
    echo "  3) Start cron service"
    echo "  4) Test launch NOW"
    echo "  5) Full auto-install (1+2+3)"
    echo "  6) Exit"
    echo ""
    read -p "  Choice [1-6]: " choice

    case "$choice" in
        1) install_deps ;;
        2) setup_cron ;;
        3) start_cron ;;
        4) test_launch ;;
        5)
            install_deps
            setup_cron
            start_cron
            echo ""
            echo -e "${GREEN}${BOLD}Done!${NC}"
            echo "  DingTalk will auto-launch:"
            echo "    8:45 Mon-Fri (check-in)"
            echo "    19:00 Mon-Fri (check-out)"
            echo ""
            echo "  Test: bash $0 checkin"
            echo "  Logs: cat $LOG"
            ;;
        6) echo "Bye!"; exit 0 ;;
        *) echo "Invalid" ;;
    esac
    echo ""
    read -p "Press Enter to continue..."
    main_menu
}

# ---- Entry ----
case "${1:-menu}" in
    checkin)  launch_dingtalk "CheckIn  08:45" ;;
    checkout) launch_dingtalk "CheckOut 19:00" ;;
    menu)     main_menu ;;
    *)        echo "Usage: $0 [checkin|checkout|menu]"; main_menu ;;
esac
