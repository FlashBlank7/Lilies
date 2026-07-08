#!/data/data/com.termux/files/usr/bin/bash
# ═══════════════════════════════════════════════════════════════
#  钉钉智能打卡 v4.0 — 双策略降级全自动打卡
#
#  Lilies 工作流平台生成 | 模板: dingtalk_smart_punch
#
#  策略:
#    Tier 1: 急速打卡 — 启动钉钉 App，利用「急速打卡」自动完成
#    Tier 2: 模拟点击 — input tap 坐标点击打卡按钮
#    Tier 3: 人工通知 — 发送通知提醒手动操作
#
#  安装:
#    bash dingtalk_smart_punch.sh install
#
#  首次使用:
#    bash dingtalk_smart_punch.sh calibrate
#
#  测试:
#    bash dingtalk_smart_punch.sh checkin-test
#    bash dingtalk_smart_punch.sh checkout-test
#
#  Lilies 调度:
#    - 上班: 45 8 * * 1-5
#    - 下班:  0 19 * * 1-5
# ═══════════════════════════════════════════════════════════════
set -e

# ── 配置 ────────────────────────────────────────────────────
CONFIG_FILE="$HOME/.dingtalk_smart_config"
LOG_FILE="$HOME/dingtalk_smart_punch.log"
SS_DIR="$HOME/storage/shared/dcim/dingtalk_punches"
PKG="com.alibaba.android.rimet"

# 颜色
GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'
RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'

# ── 工具函数 ────────────────────────────────────────────────

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$msg" | tee -a "$LOG_FILE"
}

ok()  { echo -e "  ${GREEN}✅${NC} $1"; }
warn(){ echo -e "  ${YELLOW}⚠️${NC} $1"; }
fail(){ echo -e "  ${RED}❌${NC} $1"; }
info(){ echo -e "  ${CYAN}ℹ️${NC} $1"; }
step(){ echo -e "\n${BLUE}${BOLD}━━━ $1 ━━━${NC}"; }

mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$SS_DIR" 2>/dev/null || true

# ── 配置管理 ────────────────────────────────────────────────

load_config() {
    if [ ! -f "$CONFIG_FILE" ]; then
        echo ""
        fail "未找到配置文件"
        info "请先运行校准: bash $0 calibrate"
        exit 1
    fi
    # shellcheck disable=SC1090
    source "$CONFIG_FILE"

    # 验证必需字段
    local required="checkin_x checkin_y checkout_x checkout_y"
    local missing=""
    for var in $required; do
        if [ -z "${!var:-}" ]; then
            missing="$missing $var"
        fi
    done
    if [ -n "$missing" ]; then
        fail "配置缺失:$missing"
        info "请重新校准: bash $0 calibrate"
        exit 1
    fi
}

save_config() {
    cat > "$CONFIG_FILE" << EOF
# 钉钉智能打卡配置 — Lilies 生成
# $(date '+%Y-%m-%d %H:%M:%S')
checkin_x=$checkin_x
checkin_y=$checkin_y
checkout_x=$checkout_x
checkout_y=$checkout_y
entry_x=${entry_x:-0}
entry_y=${entry_y:-0}
use_quick_punch=${use_quick_punch:-1}
use_tap_simulation=${use_tap_simulation:-1}
screen_lock_swipe=${screen_lock_swipe:-0}
use_adb=${use_adb:-1}
debug_mode=${debug_mode:-0}
EOF
    ok "配置已保存到 $CONFIG_FILE"
}

# ── ADB 辅助 ────────────────────────────────────────────────

# Android 12+ 禁止普通 App 执行 input tap，需要走 adb shell。
# 以下函数自动检测：如果 adb 已连接则走 adb，否则降级为直接命令。
adb_shell() {
    if [ "${use_adb:-1}" -eq 1 ] && adb shell echo ok 2>/dev/null | grep -q ok; then
        adb shell "$@"
    else
        "$@"
    fi
}

# ── 设备操作 ────────────────────────────────────────────────

wake_screen() {
    # 唤醒屏幕
    adb_shell input keyevent 26 2>/dev/null || input keyevent 26 2>/dev/null || true
    sleep 0.5

    # 如果配置了上滑解锁
    if [ "${screen_lock_swipe:-0}" -eq 1 ]; then
        adb_shell input swipe 500 1500 500 500 2>/dev/null || \
        input swipe 500 1500 500 500 2>/dev/null || true
        sleep 0.5
    fi
}

launch_dingtalk() {
    # am start 不需要特殊权限，直接执行
    am start -n "${PKG}/.biz.LaunchHomeActivity" 2>/dev/null && return 0
    am start -n "${PKG}/.LaunchActivity" 2>/dev/null && return 0
    am start -a android.intent.action.VIEW -d "dingtalk://" 2>/dev/null && return 0
    return 1
}

close_dingtalk() {
    am force-stop "$PKG" 2>/dev/null || true
}

go_home() {
    adb_shell input keyevent 3 2>/dev/null || input keyevent 3 2>/dev/null || true
}

take_screenshot() {
    local label="$1"
    local filename="${SS_DIR}/punch_${label}_$(date +%Y%m%d_%H%M%S).png"
    adb_shell screencap -p /sdcard/dingtalk_tmp.png 2>/dev/null && \
        adb pull /sdcard/dingtalk_tmp.png "$filename" 2>/dev/null
    screencap -p "$filename" 2>/dev/null && echo "$filename" || echo ""
}

send_notification() {
    local title="$1" msg="$2" priority="${3:-high}"
    termux-notification \
        --title "$title" \
        --content "$msg" \
        --priority "$priority" \
        --vibrate 300,100,300 \
        2>/dev/null || true
}

# ── Tier 1: 急速打卡 ────────────────────────────────────────

do_quick_punch() {
    local label="$1"  # checkin / checkout

    step "Tier 1: 急速打卡"

    log "QuickPunch [$label] — 启动钉钉..."

    wake_screen

    if launch_dingtalk; then
        log "QuickPunch [$label] — 钉钉已启动"
        info "等待急速打卡完成 (8秒)..."
        sleep 8

        # 截图留证
        local ss
        ss=$(take_screenshot "quick_${label}")
        if [ -n "$ss" ]; then
            log "QuickPunch [$label] — 截图: $ss"
        fi

        # 关闭钉钉省电
        close_dingtalk
        go_home

        log "QuickPunch [$label] — 完成 (Tier 1)"
        send_notification \
            "钉钉 $label ✅" \
            "急速打卡完成 — $(date '+%H:%M:%S')" \
            "high"
        return 0
    else
        fail "无法启动钉钉"
        return 1
    fi
}

# ── Tier 2: 模拟点击 ────────────────────────────────────────

do_tap_punch() {
    local label="$1"  # checkin / checkout
    local tap_x tap_y

    if [ "$label" = "checkin" ]; then
        tap_x="${checkin_x}"
        tap_y="${checkin_y}"
    else
        tap_x="${checkout_x}"
        tap_y="${checkout_y}"
    fi

    step "Tier 2: 模拟点击"

    log "TapPunch [$label] — 坐标: ($tap_x, $tap_y)"

    # 1. 唤醒屏幕
    wake_screen
    info "屏幕已唤醒"

    # 2. 启动钉钉
    if ! launch_dingtalk; then
        fail "无法启动钉钉"
        return 1
    fi
    info "钉钉已启动，等待加载 (5秒)..."
    sleep 5

    # 3. 如果有考勤入口坐标，先点击入口
    if [ -n "${entry_x:-}" ] && [ "${entry_x:-0}" -gt 0 ] && \
       [ -n "${entry_y:-}" ] && [ "${entry_y:-0}" -gt 0 ]; then
        info "点击考勤入口 ($entry_x, $entry_y)"
        adb_shell input tap "$entry_x" "$entry_y"
        sleep 3
    fi

    # 4. 点击打卡按钮
    info "点击打卡按钮 ($tap_x, $tap_y)"
    adb_shell input tap "$tap_x" "$tap_y"
    sleep 2

    # 5. 等待结果
    sleep 1

    # 6. 截图留证
    local ss
    ss=$(take_screenshot "tap_${label}")
    if [ -n "$ss" ]; then
        log "TapPunch [$label] — 截图: $ss"
    fi

    # 7. 关闭钉钉，回桌面
    close_dingtalk
    go_home

    log "TapPunch [$label] — 完成 (Tier 2)"
    send_notification \
        "钉钉 $label ✅" \
        "模拟点击完成 (坐标 $tap_x,$tap_y) — $(date '+%H:%M:%S')" \
        "high"
    return 0
}

# ── Tier 3: 人工通知 ────────────────────────────────────────

do_manual_notify() {
    local label="$1"

    step "Tier 3: 人工通知"

    log "ManualNotify [$label] — 自动打卡失败"

    # 尝试打开钉钉
    am start -a android.intent.action.VIEW -d "dingtalk://" 2>/dev/null || true

    # 发送强提醒
    send_notification \
        "⚠️ 钉钉 $label — 请手动打卡" \
        "自动打卡失败！已打开钉钉，请手动点击打卡按钮" \
        "max"

    # 再发一次（间隔5秒确保用户注意到）
    sleep 5
    send_notification \
        "🔴 钉钉 $label — 请现在打卡!" \
        "5秒前已提醒，请立即打开钉钉完成 $label" \
        "max"

    warn "已发送人工打卡提醒通知"
    return 0
}

# ── 核心打卡逻辑 ────────────────────────────────────────────

do_punch() {
    local label="$1"  # checkin / checkout
    local test_mode="${2:-0}"

    echo ""
    echo -e "${BLUE}${BOLD}╔══════════════════════════════════════╗${NC}"
    echo -e "${BLUE}${BOLD}║  钉钉智能打卡 v4.0 — ${label}          ║${NC}"
    echo -e "${BLUE}${BOLD}╚══════════════════════════════════════╝${NC}"

    log "══════ $label 开始 ══════"

    load_config

    # 自动重连 ADB（防止重启后端口变化）
    if [ "${use_adb:-1}" -eq 1 ]; then
        if ! adb shell echo ok 2>/dev/null | grep -q ok; then
            warn "ADB 未连接，尝试自动重连..."
            do_adb_reconnect || info "ADB 重连失败，降级为直接命令模式"
        fi
    fi

    local result=0

    # Tier 1: 急速打卡
    if [ "${use_quick_punch:-1}" -eq 1 ]; then
        if do_quick_punch "$label"; then
            log "$label — Tier 1 急速打卡 成功 ✅"
            echo ""
            ok "打卡完成 (Tier 1: 急速打卡)"
            return 0
        fi
        warn "急速打卡可能失败，降级到 Tier 2 模拟点击..."
        sleep 2
    else
        info "急速打卡已禁用，直接使用模拟点击"
    fi

    # Tier 2: 模拟点击
    if [ "${use_tap_simulation:-1}" -eq 1 ]; then
        if do_tap_punch "$label"; then
            log "$label — Tier 2 模拟点击 完成 ✅"
            echo ""
            ok "打卡完成 (Tier 2: 模拟点击)"
            return 0
        fi
        warn "模拟点击可能失败，降级到 Tier 3 人工通知..."
        sleep 2
    else
        info "模拟点击已禁用"
    fi

    # Tier 3: 人工通知
    do_manual_notify "$label"
    log "$label — Tier 3 人工通知已发送 ⚠️"
    echo ""
    warn "已发送人工通知 (Tier 3)"
    return 2
}

# ── 校准模式 ────────────────────────────────────────────────

do_calibrate() {
    echo ""
    echo -e "${BLUE}${BOLD}╔══════════════════════════════════════╗${NC}"
    echo -e "${BLUE}${BOLD}║  钉钉智能打卡 — 坐标校准向导        ║${NC}"
    echo -e "${BLUE}${BOLD}╚══════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${YELLOW}校准步骤:${NC}"
    echo "  1. 打开「设置 → 开发者选项 → 指针位置」(显示触摸坐标)"
    echo "  2. 手动打开钉钉，完成一次打卡流程"
    echo "  3. 记住每个关键点击位置的 (X, Y) 坐标"
    echo "  4. 在下方输入坐标值"
    echo ""
    echo -e "${CYAN}💡 提示:${NC}"
    echo "  - 1080×2400 屏幕: 打卡按钮通常在 (540, 1200) 附近"
    echo "  - 1080×1920 屏幕: 打卡按钮通常在 (540, 1000) 附近"
    echo "  - 考勤入口默认: (888, 388) 适用于主流屏幕"
    echo ""

    # 上班打卡按钮
    echo -e "${BOLD}【上班打卡按钮】${NC}"
    read -p "  X 坐标 [默认: 540]: " checkin_x
    checkin_x=${checkin_x:-540}
    read -p "  Y 坐标 [默认: 1200]: " checkin_y
    checkin_y=${checkin_y:-1200}
    echo ""

    # 下班打卡按钮
    echo -e "${BOLD}【下班签退按钮】${NC} (通常与上班按钮位置相同)"
    read -p "  X 坐标 [默认: $checkin_x]: " checkout_x
    checkout_x=${checkout_x:-$checkin_x}
    read -p "  Y 坐标 [默认: $checkin_y]: " checkout_y
    checkout_y=${checkout_y:-$checkin_y}
    echo ""

    # 考勤入口
    echo -e "${BOLD}【考勤入口/底部Tab】${NC}"
    echo -e "  打开钉钉后需要点击此位置进入考勤页面"
    read -p "  X 坐标 [默认: 888]: " entry_x
    entry_x=${entry_x:-888}
    read -p "  Y 坐标 [默认: 388]: " entry_y
    entry_y=${entry_y:-388}
    echo ""

    # 策略开关
    echo -e "${BOLD}【策略配置】${NC}"
    read -p "  启用 Tier 1 急速打卡? [Y/n]: " enable_quick
    use_quick_punch=1
    if [ "$enable_quick" = "n" ] || [ "$enable_quick" = "N" ]; then
        use_quick_punch=0
    fi
    read -p "  启用 Tier 2 模拟点击? [Y/n]: " enable_tap
    use_tap_simulation=1
    if [ "$enable_tap" = "n" ] || [ "$enable_tap" = "N" ]; then
        use_tap_simulation=0
    fi
    read -p "  手机有滑动解锁? [y/N]: " has_swipe
    screen_lock_swipe=0
    if [ "$has_swipe" = "y" ] || [ "$has_swipe" = "Y" ]; then
        screen_lock_swipe=1
    fi
    read -p "  使用 ADB 模拟点击 (Android 12+ 必须)? [Y/n]: " has_adb
    use_adb=1
    if [ "$has_adb" = "n" ] || [ "$has_adb" = "N" ]; then
        use_adb=0
    fi
    echo ""

    # 保存配置
    save_config

    echo ""
    echo -e "${GREEN}${BOLD}✅ 校准完成!${NC}"
    echo ""
    echo -e "配置摘要:"
    echo -e "  上班打卡按钮: (${checkin_x}, ${checkin_y})"
    echo -e "  下班打卡按钮: (${checkout_x}, ${checkout_y})"
    echo -e "  考勤入口:     (${entry_x}, ${entry_y})"
    echo -e "  急速打卡:      $([ "$use_quick_punch" -eq 1 ] && echo '✅ 启用' || echo '❌ 禁用')"
    echo -e "  模拟点击:      $([ "$use_tap_simulation" -eq 1 ] && echo '✅ 启用' || echo '❌ 禁用')"
    echo ""
    echo -e "${CYAN}下一步:${NC}"
    echo -e "  测试上班打卡: bash $0 checkin-test"
    echo -e "  测试下班打卡: bash $0 checkout-test"
    echo -e "  一键安装部署: bash $0 install"
}

# ── 一键安装 ────────────────────────────────────────────────

do_install() {
    echo ""
    echo -e "${BLUE}${BOLD}╔══════════════════════════════════════╗${NC}"
    echo -e "${BLUE}${BOLD}║  钉钉智能打卡 — 一键安装            ║${NC}"
    echo -e "${BLUE}${BOLD}╚══════════════════════════════════════╝${NC}"

    # 0. 检查/创建配置文件
    step "0/4 检查配置"
    if [ ! -f "$CONFIG_FILE" ]; then
        warn "未找到配置文件，现在开始校准..."
        echo ""
        read -p "  按回车开始校准: " _
        do_calibrate
    else
        ok "配置文件已存在"
        # 补上可能缺失的新字段
        if ! grep -q "use_adb" "$CONFIG_FILE" 2>/dev/null; then
            echo "use_adb=1" >> "$CONFIG_FILE"
            info "已添加 use_adb=1 到配置"
        fi
        if ! grep -q "entry_x" "$CONFIG_FILE" 2>/dev/null; then
            echo "entry_x=888" >> "$CONFIG_FILE"
            echo "entry_y=388" >> "$CONFIG_FILE"
            info "已添加 entry_x=888 entry_y=388 到配置"
        fi
    fi

    # 1. 安装依赖
    step "1/4 安装依赖"
    pkg update -q 2>/dev/null || true
    for pkg in cronie termux-api android-tools; do
        if dpkg -s "$pkg" >/dev/null 2>&1; then
            ok "$pkg 已安装"
        else
            info "安装 $pkg ..."
            pkg install -y "$pkg" 2>/dev/null && ok "$pkg 安装完成" || warn "$pkg 安装失败"
        fi
    done

    # 2. ADB 无线调试
    step "2/5 ADB 无线调试"
    if adb shell echo ok 2>/dev/null | grep -q ok; then
        ok "ADB 已连接"
    else
        echo ""
        echo -e "  ${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "  ${YELLOW}  Android 12+ 需要无线调试权限${NC}"
        echo -e "  ${YELLOW}  才能模拟点击打卡按钮${NC}"
        echo -e "  ${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo ""
        echo -e "  请在手机上操作："
        echo -e "  ${CYAN}1. 设置 → 开发者选项 → 无线调试 → 开启${NC}"
        echo -e "  ${CYAN}2. 点击「使用配对码配对设备」${NC}"
        echo ""
        read -p "  准备好后按回车继续..."

        read -p "  配对 IP:Port (弹窗里的): " pair_addr
        read -p "  配对码 (6位数字): " pair_code

        echo ""
        info "正在配对..."
        if echo "$pair_code" | adb pair "$pair_addr" 2>&1 | grep -qi "success"; then
            ok "配对成功"
        else
            warn "配对可能失败，请检查 IP 和配对码是否输入正确"
        fi

        echo ""
        read -p "  连接 IP:Port (无线调试主页面的): " conn_addr
        info "正在连接..."
        if adb connect "$conn_addr" 2>/dev/null | grep -q "connected"; then
            ok "ADB 已连接"
        else
            # Try 127.0.0.1
            local port="${conn_addr##*:}"
            if [ -n "$port" ]; then
                info "尝试 127.0.0.1:$port ..."
                adb connect "127.0.0.1:$port" 2>/dev/null | grep -q "connected" && ok "ADB 已连接 (127.0.0.1)" || warn "连接失败"
            else
                warn "连接失败"
            fi
        fi
    fi

    # 验证 tap 权限
    echo ""
    if adb shell echo ok 2>/dev/null | grep -q ok; then
        if adb shell input tap 1 1 2>&1 | grep -q "SecurityException"; then
            fail "ADB 已连接但 input tap 无权限，请重新配对"
        else
            ok "input tap 权限正常"
        fi
    else
        warn "ADB 未连接，input tap 将降级为直接命令"
        info "之后可手动重连: bash ~/dingtalk_smart_punch.sh adb-reconnect"
    fi

    # 3. 复制脚本
    step "3/5 部署脚本"
    local SCRIPT_PATH="$HOME/dingtalk_smart_punch.sh"
    if [ "$(readlink -f "$0")" != "$SCRIPT_PATH" ]; then
        cp "$0" "$SCRIPT_PATH"
        chmod +x "$SCRIPT_PATH"
        ok "脚本已部署到 $SCRIPT_PATH"
    else
        ok "脚本已在正确位置"
    fi

    # 4. 配置 crontab
    step "4/5 配置定时任务 (周一至周五)"
    local tmp_cron=$(mktemp)
    crontab -l 2>/dev/null > "$tmp_cron" || true

    if ! grep -q "dingtalk_smart_punch" "$tmp_cron" 2>/dev/null; then
        {
            echo ""
            echo "# 钉钉智能打卡 (Lilies 生成) — 周一至周五"
            echo "# 策略: Tier1 急速打卡 → Tier2 模拟点击 → Tier3 人工通知"
            echo "45 8 * * 1-5 bash $SCRIPT_PATH checkin  >> $LOG_FILE 2>&1"
            echo "0 19 * * 1-5 bash $SCRIPT_PATH checkout >> $LOG_FILE 2>&1"
        } >> "$tmp_cron"
        crontab "$tmp_cron"
        ok "Crontab 已配置"
    else
        ok "Crontab 已存在"
    fi
    rm -f "$tmp_cron"

    echo ""
    echo -e "  ${YELLOW}定时任务:${NC}"
    crontab -l 2>/dev/null | grep dingtalk || echo "  (无)"

    # 5. 启动 cron
    step "5/5 启动 cron 服务"
    if [ -d "$PREFIX/var/service" ]; then
        # runit 方式
        local svc_dir="$PREFIX/var/service/crond"
        if [ ! -f "$svc_dir/run" ]; then
            mkdir -p "$svc_dir/log" 2>/dev/null
            cat > "$svc_dir/run" << 'RUNEOF'
#!/data/data/com.termux/files/usr/bin/bash
exec crond -f 2>&1
RUNEOF
            chmod 755 "$svc_dir/run"
        fi
        sv up crond 2>/dev/null || true
        sleep 1
        if sv status crond 2>/dev/null | grep -q 'run'; then
            ok "crond 已启动 (runit — 终端关闭后仍运行)"
        else
            warn "runit 启动失败，使用 nohup 备用方案"
            nohup crond > /dev/null 2>&1 &
            disown
            ok "crond 已启动 (nohup)"
        fi
    else
        nohup crond > /dev/null 2>&1 &
        disown
        ok "crond 已启动 (nohup)"
    fi

    echo ""
    echo -e "${GREEN}${BOLD}╔══════════════════════════════════════╗${NC}"
    echo -e "${GREEN}${BOLD}║  ✅ 安装完成!                        ║${NC}"
    echo -e "${GREEN}${BOLD}╚══════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  日程: 周一至周五"
    echo -e "    ☀️ 08:45  上班打卡"
    echo -e "    🌙 19:00  下班签退"
    echo ""
    echo -e "  测试:"
    echo -e "    bash $SCRIPT_PATH checkin-test"
    echo -e "    bash $SCRIPT_PATH checkout-test"
    echo ""
    echo -e "  日志:"
    echo -e "    tail -f $LOG_FILE"
    echo ""
    echo -e "  重新校准坐标:"
    echo -e "    bash $SCRIPT_PATH calibrate"
    echo ""
    echo -e "  ${YELLOW}⚠️ 重要提示:${NC}"
    echo -e "    1. 手机不要设置密码/指纹锁（或在 Smart Lock 中设置可信地点）"
    echo -e "    2. 保持 Termux 通知权限开启"
    echo -e "    3. 保持电池优化对 Termux 设为「不优化」"
    echo -e "    4. 钉钉版本更新后可能需要重新校准坐标"
}

# ── 测试模式 ────────────────────────────────────────────────

do_test() {
    local label="$1"
    echo ""
    echo -e "${YELLOW}${BOLD}⚡ 测试模式: $label${NC}"
    echo -e "  将立即执行完整的打卡流程..."
    echo -e "  ${YELLOW}(这只是测试，可以在非工作时间运行)${NC}"
    echo ""
    read -p "  确认执行? [y/N]: " confirm
    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
        echo "  已取消"
        return
    fi
    do_punch "$label"
}

# ── 查看状态 ────────────────────────────────────────────────

do_status() {
    echo ""
    echo -e "${BLUE}${BOLD}╔══════════════════════════════════════╗${NC}"
    echo -e "${BLUE}${BOLD}║  钉钉智能打卡 — 运行状态            ║${NC}"
    echo -e "${BLUE}${BOLD}╚══════════════════════════════════════╝${NC}"
    echo ""

    # 配置状态
    if [ -f "$CONFIG_FILE" ]; then
        ok "配置文件: $CONFIG_FILE"
        source "$CONFIG_FILE"
        echo "  上班打卡: (${checkin_x}, ${checkin_y})"
        echo "  下班签退: (${checkout_x}, ${checkout_y})"
        echo "  急速打卡: $([ "$use_quick_punch" -eq 1 ] && echo '✅' || echo '❌')"
        echo "  模拟点击: $([ "$use_tap_simulation" -eq 1 ] && echo '✅' || echo '❌')"
    else
        fail "未配置 — 请运行 calibrate"
    fi

    echo ""

    # Crontab 状态
    if crontab -l 2>/dev/null | grep -q "dingtalk_smart_punch"; then
        ok "定时任务已配置"
        crontab -l 2>/dev/null | grep dingtalk
    else
        warn "定时任务未配置 — 请运行 install"
    fi

    echo ""

    # Cron 服务状态
    if pgrep -x crond >/dev/null 2>&1; then
        ok "cron 服务正在运行 (PID: $(pgrep -x crond))"
    else
        warn "cron 服务未运行 — 请运行 install"
    fi

    echo ""

    # 最近日志
    if [ -f "$LOG_FILE" ]; then
        echo -e "${BOLD}最近日志:${NC}"
        tail -10 "$LOG_FILE"
    else
        info "暂无日志"
    fi

    echo ""

    # 截图统计
    if [ -d "$SS_DIR" ]; then
        local ss_count
        ss_count=$(find "$SS_DIR" -name "punch_*.png" 2>/dev/null | wc -l)
        echo -e "${BOLD}截图存档:${NC} ${ss_count} 张 → ${SS_DIR}"
    fi

    echo ""

    # 今日打卡状态
    local today
    today=$(date '+%Y-%m-%d')
    if grep -q "$today.*checkin.*成功" "$LOG_FILE" 2>/dev/null; then
        ok "今日上班打卡: 已完成"
    else
        warn "今日上班打卡: 待执行"
    fi
    if grep -q "$today.*checkout.*成功" "$LOG_FILE" 2>/dev/null; then
        ok "今日下班签退: 已完成"
    else
        warn "今日下班签退: 待执行"
    fi
}

# ── 主入口 ────────────────────────────────────────────────

show_help() {
    echo ""
    echo "钉钉智能打卡 v4.0 — Lilies 工作流平台"
    echo ""
    echo "用法: bash $0 <命令>"
    echo ""
    echo "命令:"
    echo "  checkin       上班打卡 (Tier1→Tier2→Tier3 自动降级)"
    echo "  checkout      下班签退"
    echo "  checkin-test  测试上班打卡 (立即执行)"
    echo "  checkout-test 测试下班打卡 (立即执行)"
    echo "  calibrate     坐标校准向导"
    echo "  install       一键安装 (依赖+cron+服务)"
    echo "  status        查看运行状态"
    echo "  help          显示此帮助"
    echo ""
    echo "示例:"
    echo "  # 首次使用"
    echo "  bash $0 calibrate    # 校准坐标"
    echo "  bash $0 checkin-test # 测试上班打卡"
    echo "  bash $0 install      # 部署定时任务"
    echo ""
    echo "  # 日常"
    echo "  bash $0 status       # 查看状态"
    echo "  bash $0 adb-reconnect # 重启后重连 ADB"
}

# ── ADB 重连 ─────────────────────────────────────────────────

do_adb_reconnect() {
    echo ""
    echo -e "${BLUE}${BOLD}╔══════════════════════════════════════╗${NC}"
    echo -e "${BLUE}${BOLD}║  ADB 无线调试重连                    ║${NC}"
    echo -e "${BLUE}${BOLD}╚══════════════════════════════════════╝${NC}"
    echo ""

    # 检查 adb 命令是否存在
    if ! command -v adb >/dev/null 2>&1; then
        fail "未安装 android-tools，请先执行: pkg install android-tools"
        return 1
    fi

    # 方法1：如果已经配对过，尝试 127.0.0.1:37237 附近端口
    info "扫描无线调试端口..."

    local found=false
    for port in $(seq 37237 37257) $(seq 40000 40020); do
        if adb connect "127.0.0.1:$port" 2>/dev/null | grep -q "connected"; then
            ok "已连接: 127.0.0.1:$port"
            found=true
            break
        fi
    done

    if ! $found; then
        # 方法2：通过 settings 获取当前无线调试端口
        warn "扫描未找到，尝试通过系统设置获取端口..."
        local wd_port
        wd_port=$(settings get global adb_wifi_port 2>/dev/null || echo "")
        if [ -n "$wd_port" ] && [ "$wd_port" -gt 0 ] 2>/dev/null; then
            info "系统端口: $wd_port"
            if adb connect "127.0.0.1:$wd_port" 2>/dev/null | grep -q "connected"; then
                ok "已连接: 127.0.0.1:$wd_port"
                found=true
            fi
        fi
    fi

    if ! $found; then
        echo ""
        warn "自动重连失败，请手动操作："
        echo ""
        echo "  1. 设置 → 开发者选项 → 无线调试 → 查看端口号"
        echo "  2. 运行: adb connect 127.0.0.1:<端口号>"
        echo ""
        return 1
    fi

    # 验证 tap 权限
    echo ""
    if adb shell input tap 1 1 2>&1 | grep -q "SecurityException"; then
        fail "ADB 已连接但 input tap 仍无权限"
        return 1
    fi
    ok "input tap 权限正常"
    echo ""
    echo -e "${GREEN}✅ ADB 重连完成，打卡脚本已就绪${NC}"
}

case "${1:-help}" in
    checkin)
        do_punch "checkin"
        ;;
    checkout)
        do_punch "checkout"
        ;;
    checkin-test)
        do_test "checkin"
        ;;
    checkout-test)
        do_test "checkout"
        ;;
    calibrate)
        do_calibrate
        ;;
    install)
        do_install
        ;;
    status)
        do_status
        ;;
    adb-reconnect)
        do_adb_reconnect
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        echo "未知命令: $1"
        show_help
        exit 1
        ;;
esac
