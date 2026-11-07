#!/bin/sh
# ============================================================================
# oes-boot-sync — OES 启动分区守护 / 引导同步
# ----------------------------------------------------------------------------
# 背景：OES 的 u-boot 固定从 eMMC 的启动分区（/proc/cmdline 里的 ubootpart，
#       通常为 /dev/mmcblk1p1）加载 kernelfile / initrdfile / fdtfile，
#       再用 fnEnv.txt 里的 rootdev= 决定挂载哪个根分区。
#       当系统运行在 SSD 上时，OTA 只会更新 SSD 的 /boot，
#       eMMC 启动分区仍是旧内核 → 新用户态 + 旧内核（缺 trimacl 等特性）
#       → 挂载失败 / 网络起不来，表现为"升级后失联"。
#
# 本脚本的职责：
#   sync    让"实际引导分区"与"当前运行系统 /boot"保持一致（内核/initrd/dtb
#           /fnEnv.txt），并按策略修正 rootdev 指向。
#   repair  当设备启动到了 eMMC（回退态）但策略要求从 SSD 启动时，
#           用 SSD 的启动分区反向同步 eMMC，并把 rootdev 指回 SSD。
#   status  输出 key=value 形式的现状，供 Web 控制台解析。
#
# 依赖：仅需要 sh + awk + sed + coreutils + mount/blkid（fnOS 自带）。
#       不依赖 python、不依赖应用目录（关机时 /vol* 可能已卸载）。
# ============================================================================
set -u

VERSION="1.1.20"
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

CONF=/etc/oesboot.conf
RUN=/run/oes-boot-sync
LOG=/var/log/oesboot-guard.log
LOCKFILE=/run/oes-boot-sync.lock
MAX_LOG=262144

# ---- 默认配置 -------------------------------------------------------------
ENABLED=1
LOCK=follow            # follow | ssd | emmc
AUTO_REPAIR=1
AUTO_REBOOT=0
LOADER_PART=""
TARGET_BASE=""
KEEP_PREV=1
SYNC_REASON="manual"   # manual | hook | timer | watch | boot | shutdown
MIN_AGE=3              # 启动文件落盘后至少静置多少秒才复制（防半截内核）
OTA_YIELD=1            # 系统升级安装期间，周期任务自动让路（不碰引导分区）
UPGRADE_FLAG=/run/oesboot-upgrade   # 升级窗口标志（tmpfs，重启即消失）
OTA_WINDOW_MAX=10800   # 窗口最长 3 小时，超过视为残留标志，不再无限期让路
# 持久归档目录：/run 一重启就清空，升级结论和现场必须落在磁盘上。
OTA_PERSIST=/var/lib/oesboot
OTA_BASELINE="$OTA_PERSIST/ota-baseline.txt"   # 开窗那一刻的版本/内核
OTA_LAST="$OTA_PERSIST/ota-last.txt"           # 上一轮升级的结算结果
OTA_REPORT="$OTA_PERSIST/ota-report.txt"       # 现场报告（可一键导出）
OTA_SPOOL="$OTA_PERSIST/spool"                 # 窗口内抄下来的日志原件

[ -f "$CONF" ] && . "$CONF"

mkdir -p "$RUN" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
log() {
    msg="$(date '+%Y-%m-%d %H:%M:%S') [oesboot] $*"
    printf '%s\n' "$msg"
    [ -f "$LOG" ] && sz=$(wc -c < "$LOG" 2>/dev/null || echo 0)
    if [ "${sz:-0}" -gt "$MAX_LOG" ] 2>/dev/null; then
        tail -c 65536 "$LOG" > "$LOG.tmp" 2>/dev/null && mv -f "$LOG.tmp" "$LOG"
    fi
    printf '%s\n' "$msg" >> "$LOG" 2>/dev/null || true
}

die() { log "FAIL: $*"; exit 1; }

need_cmd() { command -v "$1" >/dev/null 2>&1; }

# 取挂载点对应的源设备（/dev/... 或 /dev/mapper/...）
mnt_src() { findmnt -n -o SOURCE --target "$1" 2>/dev/null | head -n1; }

# 解析设备真实的底层分区（处理 /dev/mapper/xxx → /dev/dm-N → /dev/sda2）
real_dev() {
    d="$1"
    [ -n "$d" ] || return 1
    case "$d" in
        /dev/mapper/*) d=$(readlink -f "$d" 2>/dev/null || echo "$d") ;;
    esac
    echo "$d"
}

partuuid_of() {
    blkid -s PARTUUID -o value "$(real_dev "$1")" 2>/dev/null
}

# PARTUUID 的磁盘基址（前 8 位十六进制）
base_of() {
    printf '%s' "$1" | sed -n 's/^\([0-9a-fA-F]\{8\}\)-.*/\1/p'
}

# 磁盘名 → 是否为 eMMC
is_emmc_disk() {
    case "$1" in
        mmcblk[0-9]*) return 0 ;;
        *) return 1 ;;
    esac
}

# 设备所属磁盘名
pkname() { lsblk -dn -o PKNAME "$(real_dev "$1")" 2>/dev/null | head -n1; }

# 分区是否存在
dev_exists() { [ -b "$1" ]; }

# 在系统内按 PARTUUID 查设备
dev_by_partuuid() {
    blkid -t "PARTUUID=$1" -o device 2>/dev/null | head -n1
}

# 估算文件大小（KB）
size_kb() { [ -f "$1" ] && du -k "$1" 2>/dev/null | awk '{print $1}' || echo 0; }

# 可用空间（KB）
avail_kb() { df -kP "$1" 2>/dev/null | awk 'NR==2{print $4}'; }

# 文件指纹：大小 + 修改时间（秒）
file_stamp() {
    [ -f "$1" ] || { echo "-"; return; }
    set -- "$1" "$(stat -c '%s %Y' "$1" 2>/dev/null)"
    [ -n "$2" ] && echo "$2" || echo "-"
}

# 文件距上次修改的秒数
file_age() {
    [ -f "$1" ] || { echo 9999; return; }
    m=$(stat -c '%Y' "$1" 2>/dev/null) || { echo 9999; return; }
    now=$(date +%s)
    echo $((now - m))
}

# ---------------------------------------------------------------------------
# 系统升级（OTA）是否正在安装
# ---------------------------------------------------------------------------
# OTA 安装阶段会自己去动引导分区。此时如果周期任务并发挂载 / 写 fnEnv，
# 轻则两边互相打断（OTA 报"安装失败"），重则把 OTA 写到一半的内核搬进 eMMC。
# 所以周期触发（watch / timer）检测到 OTA 在跑时直接让路，一次性触发
# （hook / shutdown / boot / manual）不受影响 —— 钩子本来就是 OTA 自己调用的。
OTA_HOLD="$RUN/ota-hold"

ota_busy() {
    [ "${OTA_YIELD:-1}" = "1" ] || return 1
    _self=$$
    for d in /proc/[0-9]*; do
        _pid=${d#/proc/}
        [ "$_pid" = "$_self" ] && continue
        _comm=$(cat "$d/comm" 2>/dev/null) || continue
        # 只认 Debian 包管理器的安装进程：它们只在安装期间存在。
        # 不能按名字里的 "update"/"apt" 泛匹配，fnOS 有常驻服务就叫这类名字，
        # 一旦误判就会长期让路、升级时反而没人同步。
        case "$_comm" in
            dpkg|dpkg-deb|apt|apt-get|unattended-upgr)
                OTA_PID="$_pid"
                OTA_CMD="$_comm"
                return 0 ;;
        esac
    done
    return 1
}

# 本轮机确实在让路（供界面区分"窗口开着在等升级"和"升级正在跑、已让路"）
ota_yield_touch() {
    mkdir -p "$RUN" 2>/dev/null
    date '+%s' > "$RUN/holding" 2>/dev/null || true
}

# 记录/清理让路状态，供 Web 控制台展示
# 返回 0=本轮让路，1=已让路太久（疑似误检测），调用方应继续同步
ota_hold_mark() {
    _now=$(date '+%Y-%m-%d %H:%M:%S')
    _since="$_now"; _n=1
    if [ -f "$OTA_HOLD" ]; then
        _since=$(env_get "$OTA_HOLD" since)
        _n=$(env_get "$OTA_HOLD" count)
        case "$_n" in ''|*[!0-9]*) _n=0 ;; esac
        _n=$((_n + 1))
        rm -f "$OTA_HOLD"
    else
        log "HOLD: 检测到系统升级正在安装（$OTA_CMD pid=$OTA_PID），暂停引导同步，升级结束后自动补同步"
    fi

    # 让路太多次说明可能把常驻进程当成了升级器。宁可同步一次，也不能一直不同步。
    if [ "$_n" -ge "${OTA_MAX_HOLD:-90}" ] 2>/dev/null; then
        log "WARN: 已连续让路 ${_n} 次（约 $((_n * 20 / 60)) 分钟），疑似误检测，本轮强制同步一次"
        return 1
    fi

    mkdir -p "$RUN" 2>/dev/null
    {
        echo "since=$_since"
        echo "last=$_now"
        echo "count=$_n"
        echo "pid=$OTA_PID"
        echo "cmd=$OTA_CMD"
    } > "$OTA_HOLD" 2>/dev/null || true
    return 0
}

ota_hold_clear() {
    rm -f "$RUN/holding" 2>/dev/null
    [ -f "$OTA_HOLD" ] || return 0
    _n=$(env_get "$OTA_HOLD" count)
    rm -f "$OTA_HOLD" 2>/dev/null
    log "HOLD 解除：系统升级已结束，本轮恢复同步（期间让路 ${_n:-0} 次）"
}

# ---------------------------------------------------------------------------
# 升级窗口
# ---------------------------------------------------------------------------
# 1.1.4 只在检测到 dpkg/apt 时让路，结果是"窗口开着、更新器没在包管理阶段"的
# 大部分时间里，守护仍然每 20 秒挂载一次 eMMC、写 last-sync、刷盘。
# fnOS 升级安装阶段要独占写引导分区，这种周期性插入足以让它报"安装失败"
# （本次 1.2.1000 连试四次都卡在安装阶段，日志里 73 次同步就是证据）。
# 1.1.5 起：只要在升级窗口内，周期任务一律不碰引导分区。
OTA_STATE="$RUN/ota.state"

# 当前系统的 fnOS 版本号。不挂载任何东西，只读正在运行的根分区。
# 升级成没成，最终判据就是这个字符串变没变。
sys_version() {
    for _f in /usr/trim/etc/version /etc/trim/version /etc/issue; do
        [ -f "$_f" ] || continue
        _v=$(grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)*' "$_f" 2>/dev/null | head -n1)
        [ -n "$_v" ] && { printf '%s' "$_v"; return 0; }
    done
    printf ''
}

in_upgrade_window() {
    [ "${OTA_YIELD:-1}" = "1" ] || return 1
    [ -f "$UPGRADE_FLAG" ] || return 1
    _age=$(file_age "$UPGRADE_FLAG")
    # 时钟异常（负数）按有效处理；超过 3 小时视为残留标志
    [ "${_age:-0}" -lt 0 ] 2>/dev/null && return 0
    [ "${_age:-0}" -lt "${OTA_WINDOW_MAX:-10800}" ] 2>/dev/null
}

# /boot 里的内核级文件是否正在变化（用户直接在系统里点升级、没开窗口的情况）
# 只做 stat，不挂载、不读写引导分区，零风险。
boot_changing() {
    _bd=$(boot_dir)
    [ -d "$_bd" ] || return 1
    _now=$(date +%s)
    for _f in "$_bd"/vmlinuz-* "$_bd"/uInitrd-* "$_bd"/initrd.img-* "$_bd"/fnEnv.txt; do
        [ -f "$_f" ] || continue
        _m=$(stat -c %Y "$_f" 2>/dev/null) || continue
        [ $((_now - _m)) -lt 120 ] 2>/dev/null && return 0
    done
    return 1
}

# 让路取证：记录窗口何时开始、让路多少次、窗口内引导分区有没有被改过。
# 升级再失败时，这份记录能直接指出"是没让路"还是"让路了但 OTA 没写 eMMC"。
ota_state_write() {
    # 只有真正因为"升级在跑"而跳过，才算一次让路（ota_state_write yield）。
    # 窗口开着但没有任何升级活动时，巡检只是例行检查，不该记成让路——
    # 否则用户只点了「启动升级守护」、根本没点升级，界面却显示"已让路 16 次"。
    _count_yield=0
    [ "${1:-}" = "yield" ] && _count_yield=1
    _st="$OTA_STATE"
    _now=$(date '+%Y-%m-%d %H:%M:%S')
    # 窗口起始时间以标志文件内容为准（UI 开窗时写的就是它）。
    # 不能拿"第一次巡检的时刻"当起点：那样算出来的窗口时长永远是 20 秒，
    # 升级失败后会被误判成"用户自己取消的"。
    _since=$(head -n1 "$UPGRADE_FLAG" 2>/dev/null | tr -d '\r\n')
    [ -n "${_since:-}" ] || _since="$_now"
    _n=0
    if [ -f "$_st" ]; then
        _s2=$(env_get "$_st" since)
        [ -n "${_s2:-}" ] && _since="$_s2"
        _n=$(env_get "$_st" yields)
        case "$_n" in ''|*[!0-9]*) _n=0 ;; esac
    fi
    _n=$((_n + _count_yield))

    # 基线：开窗那一刻的版本与内核。重启会清空 /run，所以持久目录另存一份，
    # 开机后据此算出"上一轮升级到底成没成"，不依赖任何人工记录。
    _bv=$(env_get "$_st" base_version)
    _bk=$(env_get "$_st" base_kernel)
    if [ -z "${_bv:-}" ] && [ -z "${_bk:-}" ]; then
        _bv=$(sys_version)
        _bk=$(uname -r 2>/dev/null)
        mkdir -p "$OTA_PERSIST" 2>/dev/null
        {
            echo "since=$_since"
            echo "version=$_bv"
            echo "kernel=$_bk"
        } > "$OTA_BASELINE.tmp" 2>/dev/null && mv -f "$OTA_BASELINE.tmp" "$OTA_BASELINE" 2>/dev/null
    fi
    # 只读已挂载的引导分区，绝不主动 mount
    _lk=""; _same="-"; _ld=""
    _lp=$(loader_part)
    [ -n "$_lp" ] && _ld=$(mnt_of_dev "$_lp")
    if [ -n "$_ld" ] && [ -f "$_ld/fnEnv.txt" ]; then
        _lk=$(env_fname "$_ld/fnEnv.txt" kernelfile)
        _bk=""
        _bd=$(boot_dir)
        [ -f "$_bd/fnEnv.txt" ] && _bk=$(env_fname "$_bd/fnEnv.txt" kernelfile)
        [ -n "$_bk" ] && { [ "$_bk" = "$_lk" ] && _same=1 || _same=0; }
    fi
    mkdir -p "$RUN" 2>/dev/null
    {
        echo "since=$_since"
        echo "last=$_now"
        echo "yields=$_n"
        echo "reason=$SYNC_REASON"
        echo "flag_age=$(file_age "$UPGRADE_FLAG")"
        echo "base_version=$_bv"
        echo "base_kernel=$_bk"
        echo "cur_version=$(sys_version)"
        echo "cur_kernel=$(uname -r 2>/dev/null)"
        echo "boot_kernel=$(env_fname "$(boot_dir)/fnEnv.txt" kernelfile)"
        echo "loader_kernel=$_lk"
        echo "same=$_same"
        echo "loader_mount=$_ld"
    } > "$_st.tmp" 2>/dev/null && mv -f "$_st.tmp" "$_st" 2>/dev/null
    return 0
}

# 升级现场抓取：窗口内每 20 秒把系统日志的增量抄一份到独立文件。
# 为什么要抄：fnOS 升级失败后可能清理/轮转自己的日志，事后就查无对证。
# 这里只读 journal 与日志文件，不挂载、不写引导分区，对升级零干扰。
# 存放在 $OTA_PERSIST 而不是 /var/log：后者会被 logrotate 和系统清理动到。
#
# 1.1.8：上一版关键词里有裸的 trim，结果抄进来全是 trim-mounts-hash 和
# eventlogger 的模板加载——fnOS 的升级器根本不往 journald 写自己的日志。
# 现在改成三条互补的路子：升级器 unit 名、warning 以上不看进程名、块设备硬错误。
OTA_TRACE="$OTA_PERSIST/ota-trace.log"
ota_trace() {
    command -v journalctl >/dev/null 2>&1 || return 0
    mkdir -p "$OTA_PERSIST" 2>/dev/null || return 0
    _cur="$RUN/ota.cursor"
    # 文件涨到 512KB 就只留后半段，别把根分区写满
    if [ -f "$OTA_TRACE" ]; then
        _sz=$(wc -c < "$OTA_TRACE" 2>/dev/null || echo 0)
        if [ "${_sz:-0}" -gt 524288 ] 2>/dev/null; then
            tail -c 262144 "$OTA_TRACE" > "$OTA_TRACE.t" 2>/dev/null && mv -f "$OTA_TRACE.t" "$OTA_TRACE"
        fi
    fi
    _since=$(cat "$_cur" 2>/dev/null)
    [ -n "$_since" ] || _since="-15 min"
    _out=$(
        # 1) 升级器自己：unit/进程名精确匹配（fnOS 用 trim_updatemgr 之类）
        journalctl --since "$_since" -o short-iso --no-pager 2>/dev/null \
            | grep -iE 'updatemgr|update-mgr|update_mgr|trim[_-]?update|osupgrade|os_?update|system[_-]?update|newos|installos' \
            | tail -n 60
        # 2) 警告及以上：不看是谁打的，只要是 warn/err 就抄——升级失败必有痕迹
        journalctl --since "$_since" -p warning -o short-iso --no-pager 2>/dev/null \
            | grep -viE 'trim-mounts-hash|Load template' \
            | tail -n 60
        # 3) 块设备与文件系统层：真正决定"装不进去"的地方
        journalctl --since "$_since" -o short-iso --no-pager 2>/dev/null \
            | grep -iE 'mmcblk|blk_update|i/o error|no space left|read-only file|btrfs.*(error|fail)|md/raid|raid[0-9]' \
            | tail -n 40
        dmesg 2>/dev/null | grep -viE 'trim-mounts-hash' | tail -n 25
    )
    # journalctl 没记录时会打 "-- No entries --"，别把它当现场存下来
    _out=$(printf '%s\n' "$_out" | grep -vE '^-- (No entries| Logs begin|Journal begins)')

    # 没内容就不落盘：20 秒一条空心跳，一小时能把日志撑成几百 KB
    if [ -n "${_out:-}" ]; then
        {
            printf '\n===== %s =====\n' "$(date '+%Y-%m-%d %H:%M:%S')"
            printf '%s\n' "$_out"
        } >> "$OTA_TRACE" 2>/dev/null
    fi
    date '+%Y-%m-%d %H:%M:%S' > "$_cur" 2>/dev/null
    return 0
}

# 文件日志增量抄录：升级器可能把日志写到 /usr/trim 下的文件里，不经过 journald。
# 按文件记录读取偏移，每轮只抄新增部分；首次最多 64KB（把历史也带上）。
OTA_TRACE_FILE="$OTA_PERSIST/ota-trace-file.log"
ota_file_trace() {
    mkdir -p "$OTA_SPOOL" 2>/dev/null || return 0
    for _f in /usr/trim/log/*.log /usr/trim/log/*/*.log /usr/trim/var/log/*.log \
              /var/log/trim/*.log /var/log/fnos*.log /var/log/*update*.log \
              /var/log/*upgrade*.log /var/log/ota*.log \
              /var/log/apt/term.log /var/log/apt/history.log /var/log/dpkg.log \
              /var/tmp/trim-update/*.log /var/tmp/trim-update/*.txt; do
        [ -f "$_f" ] || continue
        _id=$(printf '%s' "$_f" | tr '/' '_')
        _off_f="$OTA_SPOOL/$_id.off"
        _prev=$(cat "$_off_f" 2>/dev/null)
        case "$_prev" in ''|*[!0-9]*) _prev=0 ;; esac
        _sz=$(stat -c %s "$_f" 2>/dev/null || echo 0)
        case "${_sz:-0}" in ''|*[!0-9]*) continue ;; esac
        # 文件被轮转或截断：从头再来
        [ "$_sz" -lt "$_prev" ] 2>/dev/null && _prev=0
        [ "$_sz" -le "$_prev" ] 2>/dev/null && continue
        # 单轮最多 64KB，防止一次性把大文件灌进来
        if [ $((_sz - _prev)) -gt 65536 ] 2>/dev/null; then
            _prev=$((_sz - 65536))
        fi
        {
            printf '\n===== %s @ %s =====\n' "$_f" "$(date '+%Y-%m-%d %H:%M:%S')"
            tail -c +$((_prev + 1)) "$_f" 2>/dev/null | tr -d '\000'
            printf '\n'
        } >> "$OTA_TRACE_FILE" 2>/dev/null
        echo "$_sz" > "$_off_f" 2>/dev/null
    done
    # 总量控制在 1MB
    if [ -f "$OTA_TRACE_FILE" ]; then
        _tsz=$(wc -c < "$OTA_TRACE_FILE" 2>/dev/null || echo 0)
        if [ "${_tsz:-0}" -gt 1048576 ] 2>/dev/null; then
            tail -c 524288 "$OTA_TRACE_FILE" > "$OTA_TRACE_FILE.t" 2>/dev/null \
                && mv -f "$OTA_TRACE_FILE.t" "$OTA_TRACE_FILE"
        fi
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 进程与写入轨迹：日志查不到，就直接看升级器动了什么。
#
# fnOS 的升级器一句话都不往 journald / 日志文件里写（1.1.8/1.1.9 两轮取证都证实了），
# 所以只能从外部观察：谁起来了、往哪块盘写、写了多少、哪个分区在涨。
# 每 20 秒一轮，只记「变化」，正常一轮几十字节，不会把根分区写满。
OTA_PROBE="$OTA_PERSIST/ota-probe.log"
ota_probe() {
    mkdir -p "$OTA_PERSIST" 2>/dev/null || return 0
    _seen="$RUN/probe.seen"
    _now="$RUN/probe.now"
    _fdseen="$RUN/probe.fd.seen"
    : > "$_now" 2>/dev/null || return 0

    # ---- 1) 匹配进程快照：pid | write_bytes | cmdline ----
    for _p in /proc/[0-9]*; do
        _pid=${_p#/proc/}
        _cmd=$(tr '\0' ' ' < "$_p/cmdline" 2>/dev/null)
        [ -n "$_cmd" ] || continue
        case "$_cmd" in
            *oes-boot-sync*|*Load\ template*) continue ;;
            *updatemgr*|*update_mgr*|*osupgrade*|*system_update*|*mkfs*|*btrfs*|*rsync*|*dpkg*|*apt-get*|*unzip*) ;;
            *) continue ;;
        esac
        _w=$(sed -n 's/^write_bytes: *//p' "$_p/io" 2>/dev/null)
        printf '%s|%s|%s\n' "$_pid" "${_w:-0}" "$(printf '%s' "$_cmd" | cut -c1-110)" >> "$_now"
    done

    _out=""
    if [ -s "$_seen" ]; then
        _out=$(awk -F'|' '
            NR==FNR { w[$1]=$2; c[$1]=$3; next }
            !($1 in w) { printf "  [新进程] pid=%s  %s\n", $1, $3 }
            ($1 in w) && $2 - w[$1] > 2097152 { printf "  [写入+] pid=%s +%dMB  %s\n", $1, int(($2-w[$1])/1048576), c[$1] }
        ' "$_seen" "$_now" 2>/dev/null)
    else
        _out=$(awk -F'|' '{ printf "  [进程] pid=%s  %s\n", $1, $3 }' "$_now" 2>/dev/null)
    fi
    cp -f "$_now" "$_seen" 2>/dev/null

    # ---- 2) 打开了哪些块设备 / 关键目录：能直接看出它准备写哪块盘 ----
    _fdnow="$RUN/probe.fd.now"
    : > "$_fdnow" 2>/dev/null
    for _p in /proc/[0-9]*; do
        _pid=${_p#/proc/}
        _cmd=$(tr '\0' ' ' < "$_p/cmdline" 2>/dev/null)
        case "$_cmd" in
            ""|*oes-boot-sync*|*Load\ template*) continue ;;
            *updatemgr*|*osupgrade*|*mkfs*|*btrfs*|*dpkg*|*unzip*|*/trim*) ;;
            *) continue ;;
        esac
        _c=$(printf '%s' "$_cmd" | cut -c1-50)
        for _l in "$_p"/fd/*; do
            _t=$(readlink "$_l" 2>/dev/null) || continue
            case "$_t" in
                /dev/mmcblk*|/dev/sd*|/boot/*|/vol00/*) printf '  [占用] pid=%s → %s  (%s)\n' "$_pid" "$_t" "$_c" ;;
            esac
        done
    done >> "$_fdnow" 2>/dev/null
    if [ -s "$_fdnow" ]; then
        _newfd=$(sort -u "$_fdnow" 2>/dev/null)
        _oldfd=$( [ -f "$_fdseen" ] && sort -u "$_fdseen" 2>/dev/null )
        # 只报"这轮新出现"的占用，避免同一批 fd 每轮重复记
        _delta=$(printf '%s\n' "$_newfd" | grep -vxF -e "$_oldfd" -e '' 2>/dev/null)
        [ -n "$_delta" ] && _out="$_out
$_delta"
        printf '%s\n' "$_newfd" > "$_fdseen" 2>/dev/null
    fi

    # ---- 3) 各分区占用变化：OTA 到底往哪块盘装东西 ----
    _dfnow=$(df -P -k 2>/dev/null | awk 'NR>1 && $1 ~ /^\/dev\/(sd|mmcblk|mapper)/ {print $1"|"$3}')
    _dfseen="$RUN/probe.df.seen"
    if [ -f "$_dfseen" ]; then
        _dfdelta=$(printf '%s\n' "$_dfnow" | awk -F'|' '
            NR==FNR { u[$1]=$2; next }
            ($1 in u) && $2 != u[$1] { printf "  [分区] %s %dKB → %dKB (%+dMB)\n", $1, u[$1], $2, int(($2-u[$1])/1024) }
        ' "$_dfseen" - 2>/dev/null)
        [ -n "$_dfdelta" ] && _out="$_out
$_dfdelta"
    fi
    printf '%s\n' "$_dfnow" > "$_dfseen" 2>/dev/null

    # ---- 4) 安装阶段指纹：dpkg 有没有半装、新内核有没有真的落到 /boot ----
    # 升级器只吐一句 "failed to install 'linux-image-xxx'"，四个字概括完。
    # 这里每轮拍一次状态，只要变了就记一条，失败瞬间的状态一定留得住。
    _st="$RUN/probe.pkg"
    _stnow=$( {
        dpkg --audit 2>/dev/null | head -c 400
        echo "---"
        dpkg -l 2>/dev/null | awk '$1=="iF"||$1=="iU"||$1=="iH"||$1=="iR"||$1=="iE"{print $1" "$2" "$3}'
        echo "---"
        ls -1 /boot 2>/dev/null | grep -iE 'linux-image|vmlinuz|initrd|\.dpkg-' | sort
    } 2>/dev/null )
    _stold=$(cat "$_st" 2>/dev/null)
    if [ "$_stnow" != "$_stold" ]; then
        _out="$_out
  [安装态变化] $(date '+%H:%M:%S') dpkg/内核文件与上一轮不同："
        _out="$_out
$(printf '%s\n' "$_stnow" | sed 's/^/    /')"
        printf '%s\n' "$_stnow" > "$_st" 2>/dev/null
    fi

    # ---- 落盘：有变化才写 ----
    _out=$(printf '%s\n' "$_out" | grep -v '^$' 2>/dev/null)
    if [ -n "$_out" ]; then
        if [ -f "$OTA_PROBE" ]; then
            _sz=$(wc -c < "$OTA_PROBE" 2>/dev/null || echo 0)
            if [ "${_sz:-0}" -gt 524288 ] 2>/dev/null; then
                tail -c 262144 "$OTA_PROBE" > "$OTA_PROBE.t" 2>/dev/null \
                    && mv -f "$OTA_PROBE.t" "$OTA_PROBE"
            fi
        fi
        {
            printf '\n===== %s =====\n' "$(date '+%Y-%m-%d %H:%M:%S')"
            printf '%s\n' "$_out"
        } >> "$OTA_PROBE" 2>/dev/null
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 升级结算与现场归档
# ---------------------------------------------------------------------------
# 之前每次升级失败都要人手动导诊断报告，还经常导错文件。
# 这里把"成没成"和"现场在哪"全部自动落盘：
#   窗口关闭（含系统自动重启）→ 第一次 timer 触发时结算 → 生成 ota-report.txt
_window_span() {
    _t=$(date -d "$1" +%s 2>/dev/null) || { echo 0; return 0; }
    _n=$(date +%s)
    _d=$((_n - _t))
    [ "$_d" -lt 0 ] 2>/dev/null && _d=0
    echo "$_d"
}

ota_bundle() {
    mkdir -p "$OTA_PERSIST" 2>/dev/null || return 0
    # 手动抓取时窗口内可能还没跑过监视，先把两条日志抓一遍，
    # 否则报告里十二、十三段永远是空的。两个函数都只读，不改系统状态。
    ota_trace 2>/dev/null
    ota_file_trace 2>/dev/null
    ota_probe 2>/dev/null
    _rep="$OTA_REPORT.tmp"
    _lp=$(loader_part 2>/dev/null)
    _ld=""
    [ -n "${_lp:-}" ] && _ld=$(mnt_of_dev "$_lp" 2>/dev/null)
    {
        echo "# OES 升级现场报告"
        echo "生成时间: $(date '+%Y-%m-%d %H:%M:%S')"
        echo "管家版本: v$VERSION"
        echo
        echo "## 一、结算结果"
        if [ -f "$OTA_LAST" ] && [ -s "$OTA_LAST" ]; then
            sed 's/^/  /' "$OTA_LAST" 2>/dev/null
        elif in_upgrade_window; then
            echo "  state=running"
            echo "  note=升级窗口仍在进行中，尚未结算（这是实时快照）"
        else
            echo "  state=none"
            echo "  note=没有结算记录（还没跑过带升级窗口的升级）"
        fi
        echo
        echo "## 二、开窗基线与窗口"
        if [ -f "$OTA_BASELINE" ] && [ -s "$OTA_BASELINE" ]; then
            sed 's/^/  /' "$OTA_BASELINE" 2>/dev/null
        else
            echo "  （无：这一轮没有记录到开窗基线）"
        fi
        if in_upgrade_window; then
            echo "  窗口=开启中（$(file_age "$UPGRADE_FLAG")s 前打开）"
        else
            echo "  窗口=未开启"
        fi
        echo "  监视定时器=$(systemctl is-active oesboot-watch.timer 2>/dev/null || echo unknown)"
        echo "  巡检定时器=$(systemctl is-active oesboot-check.timer 2>/dev/null || echo unknown)"
        [ -f "$OTA_STATE" ] && sed 's/^/  /' "$OTA_STATE" 2>/dev/null
        echo
        echo "## 三、当前系统"
        echo "  版本: $(sys_version)"
        echo "  内核: $(uname -r 2>/dev/null)"
        echo "  运行时长: $(cut -d. -f1 /proc/uptime 2>/dev/null)s"
        echo "  内核参数: $(cat /proc/cmdline 2>/dev/null)"
        echo
        echo "## 四、空间"
        df -h 2>/dev/null | grep -vE '^(tmpfs|devtmpfs)' | sed 's/^/  /'
        echo
        echo "## 五、分区"
        if command -v lsblk >/dev/null 2>&1; then
            lsblk -f 2>/dev/null | sed 's/^/  /'
        fi
        echo
        if command -v blkid >/dev/null 2>&1; then
            blkid 2>/dev/null | sed 's/^/  /'
        fi
        echo
        echo "## 六、软 RAID"
        sed 's/^/  /' /proc/mdstat 2>/dev/null
        echo
        echo "## 七、/boot（升级源）"
        _bd=$(boot_dir 2>/dev/null)
        if [ -n "${_bd:-}" ] && [ -d "$_bd" ]; then
            ls -l "$_bd" 2>/dev/null | sed 's/^/  /'
            echo "  --- fnEnv.txt ---"
            sed 's/^/  /' "$_bd/fnEnv.txt" 2>/dev/null
        else
            echo "  （未定位到 /boot）"
        fi
        echo
        echo "## 八、引导分区 eMMC"
        echo "  设备: ${_lp:-未识别}  挂载点: ${_ld:-未挂载}"
        if [ -n "${_ld:-}" ]; then
            ls -l "$_ld" 2>/dev/null | sed 's/^/  /'
            echo "  --- fnEnv.txt ---"
            sed 's/^/  /' "$_ld/fnEnv.txt" 2>/dev/null
        fi
        echo
        echo "## 九、内核日志（dmesg 尾部）"
        dmesg 2>/dev/null | tail -n 150 | sed 's/^/  /'
        echo
        echo "## 十、journald：升级器与块设备错误（本次启动）"
        {
            journalctl -b -o short-iso --no-pager 2>/dev/null \
                | grep -iE 'updatemgr|update-mgr|update_mgr|trim[_-]?update|osupgrade|os_?update|system[_-]?update|newos|installos' \
                | tail -n 120
            journalctl -b -o short-iso --no-pager 2>/dev/null \
                | grep -iE 'mmcblk|blk_update|i/o error|no space left|read-only file|btrfs.*(error|fail)|md/raid|raid[0-9]' \
                | tail -n 80
        } | sed 's/^/  /'
        echo
        echo "## 十一、journald：警告及以上（本次 + 上次启动）"
        {
            journalctl -b -p warning -o short-iso --no-pager 2>/dev/null | tail -n 100
            echo "  --- 上一次启动 ---"
            journalctl -b -1 -p warning -o short-iso --no-pager 2>/dev/null | tail -n 100
        } | grep -viE 'trim-mounts-hash|Load template' | sed 's/^/  /'
        echo
        echo "## 十二、升级期间守护抄录（journald）"
        if [ -f "$OTA_TRACE" ]; then
            tail -c 153600 "$OTA_TRACE" 2>/dev/null | sed 's/^/  /'
        else
            echo "  （无：窗口内没有开启监视，或 journalctl 不可用）"
        fi
        echo
        echo "## 十三、升级期间守护抄录（日志文件）"
        if [ -f "$OTA_TRACE_FILE" ]; then
            tail -c 153600 "$OTA_TRACE_FILE" 2>/dev/null | sed 's/^/  /'
        else
            echo "  （无：/usr/trim 与 /var/log 下没有找到升级器的日志文件）"
        fi
        echo
        echo "## 十四、升级过程进程轨迹（谁起来了、往哪块盘写）"
        if [ -f "$OTA_PROBE" ]; then
            tail -c 153600 "$OTA_PROBE" 2>/dev/null | sed 's/^/  /'
        else
            echo "  （无：窗口内监视没有跑起来，或升级期间没有任何匹配进程）"
        fi
        echo
        echo "## 十五、引导布局结论"
        _lpb=$(loader_part 2>/dev/null)
        _bpb=$(boot_part 2>/dev/null)
        echo "  u-boot 读内核的分区: ${_lpb:-未知}（来自 /proc/cmdline 的 ubootpart）"
        echo "  /boot 实际挂载于:    ${_bpb:-未知}（$(mnt_of_dev "${_bpb:-/nonexistent}" 2>/dev/null || echo -)）"
        if [ -z "$_lpb" ]; then
            echo "  结论: 无法判定（没找到 u-boot 读取的引导分区）"
        elif [ "$(real_dev "$_lpb" 2>/dev/null)" = "$(real_dev "$_bpb" 2>/dev/null)" ]; then
            echo "  结论: 一致 —— OTA 写进 /boot 的新内核，u-boot 能直接读到"
        else
            echo "  结论: 不一致 —— OTA 写进 ${_bpb:-/boot} 的新内核，u-boot 会从 ${_lpb:-eMMC} 读旧内核，"
            echo "        升级器核对后判定「安装失败」。修复：守护页 →「修正引导布局」→ 重启。"
        fi
        echo
        echo "## 十六、安装阶段证据（dpkg / apt 原文）"
        echo "  升级器报 failed to install 时，真正的原因只写在下面两处；"
        echo "  本段直接抄原文，不再靠推断。"
        echo
        echo "  --- /var/log/apt/term.log 尾部 ---"
        if [ -s /var/log/apt/term.log ]; then
            tail -c 8192 /var/log/apt/term.log 2>/dev/null | sed 's/^/  /'
        else
            echo "  （无此文件：升级器可能不走 apt，而是自己解包）"
        fi
        echo
        echo "  --- /var/log/dpkg.log 尾部 ---"
        if [ -s /var/log/dpkg.log ]; then
            tail -n 60 /var/log/dpkg.log 2>/dev/null | sed 's/^/  /'
        else
            echo "  （无此文件）"
        fi
        echo
        echo "  --- dpkg --audit（半装 / 损坏的包） ---"
        _aud=$(dpkg --audit 2>&1)
        if [ -n "${_aud:-}" ]; then
            printf '%s\n' "$_aud" | sed 's/^/  /'
        else
            echo "  （干净：没有半装或损坏的包）"
        fi
        echo
        echo "  --- 非正常状态的包 ---"
        {
            dpkg -l 2>/dev/null | awk '$1 !~ /^(ii|rc)$/ && $1 ~ /^[a-z]/ {print}'
        } | head -n 40 | sed 's/^/  /' || echo "  （无）"
        echo
        echo "  --- 已装的 linux-image ---"
        dpkg -l 2>/dev/null | grep -E 'linux-image' | sed 's/^/  /' || echo "  （无）"
        echo
        echo "  --- 升级器工作目录 /var/tmp/trim-update ---"
        if [ -d /var/tmp/trim-update ]; then
            ls -la /var/tmp/trim-update 2>/dev/null | head -n 30 | sed 's/^/  /'
            echo "  --- 目录占用 ---"
            du -sh /var/tmp/trim-update 2>/dev/null | sed 's/^/  /'
        else
            echo "  （无：升级器已清理，或用了别的目录）"
        fi
        echo
        echo "  --- /boot 空间与 inode（vfat 无 inode，看簇） ---"
        df -h /boot 2>/dev/null | sed 's/^/  /'
        df -i /boot 2>/dev/null | sed 's/^/  /'
        echo
        echo "  --- /boot 里与新内核同名的残留 ---"
        ls -la /boot 2>/dev/null | grep -iE 'c1191|\.new$|\.dpkg|tmp' | sed 's/^/  /' || echo "  （无残留）"
        echo
        echo "## 十七、守护自身状态（为什么没自动同步）"
        echo "  「启动分区内核与系统不一致」长期不消失时，答案只在这两处："
        echo "  守护根本没跑，或者跑了但被自己的前置检查挡下来了。"
        echo
        echo "  --- 守护日志尾部（/var/log/oesboot-guard.log） ---"
        if [ -s "$LOG" ]; then
            tail -n 40 "$LOG" 2>/dev/null | sed 's/^/  /'
        else
            echo "  （日志为空：守护可能一次都没成功执行过）"
        fi
        echo
        echo "  --- 配置实际生效值（/etc/oesboot.conf） ---"
        echo "  ENABLED=$ENABLED   （0 = 守护已关闭，自动同步与钩子全部不触发）"
        echo "  LOCK=${LOCK:-follow}   OTA_YIELD=${OTA_YIELD:-1}   MIN_AGE=${MIN_AGE:-3}"
        if [ "${ENABLED:-1}" != "1" ]; then
            echo "  ⚠ 守护开关是关闭状态：单元可能是 active，但脚本每次都在开头就退出，"
            echo "    表现就是「守护运行中」却永远不出现新内核。"
        fi
        echo
        echo "  --- 单元状态 ---"
        for _u in oesboot-sync.service oesboot-shutdown-sync.service \
                  oesboot-check.timer oesboot-watch.timer; do
            printf '  %-32s active=%s enabled=%s\n' "$_u" \
                "$(systemctl is-active "$_u" 2>/dev/null)" \
                "$(systemctl is-enabled "$_u" 2>/dev/null)"
        done
        echo
        echo "  --- 内核模块是否装齐（同步前的硬门槛） ---"
        for _m in /lib/modules /usr/lib/modules; do
            [ -d "$_m" ] || { echo "  $_m: 不存在"; continue; }
            echo "  $_m: $(ls -1 "$_m" 2>/dev/null | tr '\n' ' ')"
        done
        echo "  当前运行内核: $(uname -r 2>/dev/null)"
        echo "  /boot 的 kernelfile: $(env_fname "$(boot_dir)/fnEnv.txt" kernelfile 2>/dev/null)"
        echo
        echo "  --- 上一次同步记录（引导分区 .oesboot/last-sync） ---"
        _ld2=$(mnt_of_dev "$(loader_part 2>/dev/null)" 2>/dev/null)
        if [ -n "${_ld2:-}" ] && [ -f "$_ld2/.oesboot/last-sync" ]; then
            sed 's/^/  /' "$_ld2/.oesboot/last-sync" 2>/dev/null
        else
            echo "  （没有记录：说明这台机器从未成功同步过引导分区）"
        fi
    } > "$_rep" 2>/dev/null
    [ -s "$_rep" ] && mv -f "$_rep" "$OTA_REPORT" 2>/dev/null
    rm -f "$_rep" 2>/dev/null
    return 0
}

# 结算上一轮升级。幂等：同一轮只算一次（用基线里的 since 做标识）。
ota_finalize() {
    [ -f "$OTA_BASELINE" ] || return 0
    in_upgrade_window && return 0

    _bs=$(env_get "$OTA_BASELINE" since)
    _done=$(env_get "$OTA_LAST" since)
    [ -n "${_bs:-}" ] && [ "$_bs" = "${_done:-}" ] && return 0

    _bv=$(env_get "$OTA_BASELINE" version)
    _bk=$(env_get "$OTA_BASELINE" kernel)
    _cv=$(sys_version)
    _ck=$(uname -r 2>/dev/null)
    _span=$(_window_span "${_bs:-}")

    # 窗口期每 20 秒至少会跑一次，会留下 /run 里的运行记录。
    # 但 /run 是 tmpfs —— 升级完自动重启，记录必然没了。
    # 旧逻辑把这种情况一律当成"历史残留"直接丢弃，结果是：
    # 真的升级成功了，却没有结算、也不生成现场报告，用户只看到"无结算记录"。
    # 现在改为：没有运行记录时，看版本/内核是否真的变了——
    # 变了就是"升级后重启"，照常结算；没变才当残留丢弃。
    if [ ! -f "$OTA_STATE" ]; then
        _really="0"
        [ -n "${_cv:-}" ] && [ -n "${_bv:-}" ] && [ "$_cv" != "$_bv" ] && _really="1"
        [ -n "${_ck:-}" ] && [ -n "${_bk:-}" ] && [ "$_ck" != "$_bk" ] && _really="1"
        if [ "$_really" = "1" ]; then
            log "结算：运行记录随重启丢失，但版本/内核已变（$_bv/$_bk → $_cv/$_ck），按升级后重启结算"
        elif [ "${_span:-0}" -gt 86400 ] 2>/dev/null; then
            log "结算跳过：无运行记录且基线已存在 ${_span}s，判定为历史残留，已丢弃"
            rm -f "$OTA_BASELINE" 2>/dev/null
            return 0
        fi
    fi

    _state="unknown"
    _note="缺少基线信息，无法判定"
    if [ -n "${_cv:-}" ] && [ -n "${_bv:-}" ] && [ "$_cv" != "$_bv" ]; then
        _state="success"
        _note="系统版本 $_bv → $_cv，升级已生效"
    elif [ -n "${_ck:-}" ] && [ -n "${_bk:-}" ] && [ "$_ck" != "$_bk" ]; then
        _state="success"
        _note="内核 $_bk → $_ck（版本号未变，可能只更新了内核）"
    elif [ -n "${_cv:-}" ] && [ "$_cv" = "${_bv:-}" ]; then
        if [ "${_span:-0}" -ge 300 ] 2>/dev/null; then
            _state="failed"
            _note="窗口持续 ${_span}s，系统版本仍为 $_cv，升级未生效"
        else
            _state="idle"
            _note="窗口仅 ${_span}s 就关闭，系统版本未变（多半是打开后又取消了）"
        fi
    fi

    _yields=""
    [ -f "$OTA_STATE" ] && _yields=$(env_get "$OTA_STATE" yields)

    # 失败时把升级器那句原文留下来：它只打一行 "failed to install '<pkg>'"，
    # 这一行里的包名就是下一次要盯的对象。查不到就留空，不编。
    _failpkg=""
    _failline=""
    if [ "$_state" = "failed" ]; then
        _failline=$(journalctl --since "${_bs:-}" -o short-iso --no-pager 2>/dev/null \
            | grep -iE "failed to (install|update) '" | tail -n 1 | sed 's/^[^ ]* [^ ]* //')
        [ -n "${_failline:-}" ] || _failline=$(grep -oE "failed to (install|update) '[^']+'" "$OTA_TRACE" 2>/dev/null | tail -n 1)
        _failpkg=$(printf '%s' "${_failline:-}" | sed -n "s/.*failed to [a-z]* '\([^']*\)'.*/\1/p")
    fi

    mkdir -p "$OTA_PERSIST" 2>/dev/null
    {
        echo "since=${_bs:-}"
        echo "finished=$(date '+%Y-%m-%d %H:%M:%S')"
        echo "state=$_state"
        echo "span=${_span:-0}"
        echo "version_before=${_bv:-}"
        echo "version_after=${_cv:-}"
        echo "kernel_before=${_bk:-}"
        echo "kernel_after=${_ck:-}"
        echo "yields=${_yields:-0}"
        echo "fail_pkg=${_failpkg:-}"
        echo "fail_line=${_failline:-}"
        echo "note=$_note"
    } > "$OTA_LAST.tmp" 2>/dev/null && mv -f "$OTA_LAST.tmp" "$OTA_LAST" 2>/dev/null

    # 只有真正试过升级（成功或失败）才留现场，避免"随手点一下"也生成一堆报告
    case "$_state" in
        success|failed)
            ota_bundle
            log "升级结算：$_state — $_note（现场已归档 $OTA_REPORT）"
            ;;
        *)
            log "升级结算：$_state — $_note"
            ;;
    esac
    return 0
}

# 内核模块是否装齐：新内核必须先有 /lib/modules/<ver> 才算"装完"。
# OTA 只把内核文件丢进 /boot 而没装 modules 时，把它同步过去就变成
# "新内核 + 旧用户态"，重启直接卡死 —— 这正是当初失联的镜像场景。
modules_ok() {
    _k="$1"
    case "$_k" in vmlinuz-*) ;; *) return 0 ;; esac
    _v="${_k#vmlinuz-}"
    [ -n "$_v" ] || return 0
    [ "$_v" = "$(uname -r 2>/dev/null)" ] && return 0   # 正在跑的当然完整

    # 两个目录都要看：/lib/modules 存在但空、而实际模块在 /usr/lib/modules
    # 的情况（或 /lib 不是软链的发行版）会让旧写法误判成"只装了一半"，
    # 于是新内核永远同步不过去，界面一直卡在"启动分区内核与系统不一致"。
    _seen_base=0
    for _base in /lib/modules /usr/lib/modules; do
        [ -d "$_base" ] || continue
        [ -n "$(ls -A "$_base" 2>/dev/null | head -n1)" ] || continue
        _seen_base=1
        for _d in "$_base/$_v" "$_base/$_v"-*; do
            [ -d "$_d" ] || continue
            [ -n "$(ls -A "$_d" 2>/dev/null | head -n1)" ] && return 0
        done
    done
    # 这台机器压根不用这套布局（两个目录都没有或整个是空的）→ 判据不适用，放行。
    # 宁可少挡一次，也不能因为路径不同就把正常升级永久卡住。
    [ "$_seen_base" = "1" ] || return 0
    return 1
}

# 内核文件是否像一份完整内核
# ---------------------------------------------------------------------------
# OTA 安装失败时会在 /boot 留下写了一半的内核，并可能把 fnEnv 指过去。
# 这种文件大小"稳定"、sha 也自洽，唯一能挡住它的就是格式判据。
# 只读文件头几十字节，关机路径上也够快。
kernel_sane() {
    _f="$1"
    [ -f "$_f" ] || return 1
    _sz=$(stat -c %s "$_f" 2>/dev/null || echo 0)
    case "$_sz" in ''|*[!0-9]*) return 1 ;; esac
    [ "$_sz" -ge 4194304 ] 2>/dev/null || return 1      # <4MB 不可能是内核

    # 没有 od/head 时只能信大小，退回放行（宁可同步也不误挡正常升级）
    command -v od >/dev/null 2>&1 || return 0
    _sig=$(head -c 4 "$_f" 2>/dev/null | od -An -tx1 2>/dev/null | tr -d ' \n')
    [ -n "$_sig" ] || return 0

    case "$_sig" in
        4d5a*)       return 0 ;;    # MZ  —— EFI stub / PE 镜像
        1f8b*)       return 0 ;;    # gzip
        425a68*)     return 0 ;;    # bzip2
        fd377a58*)   return 0 ;;    # xz
        28b52ffd*)   return 0 ;;    # zstd
        02214c18*)   return 0 ;;    # lz4
        5d000000*)   return 0 ;;    # lzma
    esac

    # arm64 Image：偏移 0x38 处为 "ARMd"（大端魔数 0x644d5241）
    _m2=$(dd if="$_f" bs=1 skip=56 count=4 2>/dev/null | od -An -tx1 2>/dev/null | tr -d ' \n')
    [ "$_m2" = "41524d64" ] && return 0
    # arm32 zImage：偏移 0x24 处为 0x016f2818
    _m3=$(dd if="$_f" bs=1 skip=36 count=4 2>/dev/null | od -An -tx1 2>/dev/null | tr -d ' \n')
    [ "$_m3" = "18286f01" ] && return 0

    return 1
}

# 复制单个启动文件：先写临时文件，复核源在整个复制期间未变动，再原子替换。
# 升级时 OTA 可能正把内核写进 /boot，此刻复制会拿到半截文件（大小与源一致，
# 校验也过），写进 eMMC 就是"升级后起不来"的直接原因，所以必须挡住。
# 返回 0=已复制 1=已一致 2=源未稳定（下一轮再试） 3=失败
copy_entry() {
    _s="$1"; _d="$2"; _n="$3"
    # 周期任务（监视/巡检）下一轮还会来，刚落盘的文件先放一轮；
    # 关机/开机/钩子只有一次机会，不能等，改成原地重试。
    _min="${MIN_AGE:-3}"
    _waits=3
    case "$SYNC_REASON" in
        shutdown|boot) _min=0; _waits=9 ;;
        hook|manual)   _min=0; _waits=6 ;;
    esac
    # age 为负说明系统时钟异常，不能因此永久跳过
    _age=$(file_age "$_s")
    if [ "${_age:-9999}" -ge 0 ] 2>/dev/null && [ "${_age:-9999}" -lt "$_min" ] 2>/dev/null; then
        log "WAIT: $_n 刚落盘（${_age}s 前），本轮跳过，下一轮再同步"
        return 2
    fi

    # 快速路径：大小一致且内容一致就没必要进入等待
    if [ -f "$_d" ] && [ "$(stat -c %s "$_s" 2>/dev/null)" = "$(stat -c %s "$_d" 2>/dev/null)" ] && \
       [ "$(sha256sum "$_s" 2>/dev/null | awk '{print $1}')" = "$(sha256sum "$_d" 2>/dev/null | awk '{print $1}')" ]; then
        return 1
    fi

    _tmp="$_d.oesboot-tmp"
    _round=0
    while [ "$_round" -lt 2 ]; do
        _round=$((_round + 1))
        # 必须"连续 2 秒没有变化"才算写完。只比较一次前后戳不够：
        # OTA 常分块写入，块与块之间有停顿，那一瞬间看起来是稳定的。
        _quiet=0
        _poll=0
        while [ "$_quiet" -lt 2 ] && [ "$_poll" -lt "$_waits" ]; do
            _a=$(file_stamp "$_s")
            sleep 1
            _b=$(file_stamp "$_s")
            if [ "$_a" = "$_b" ]; then _quiet=$((_quiet + 1)); else _quiet=0; fi
            _poll=$((_poll + 1))
        done
        if [ "$_quiet" -lt 2 ]; then
            log "WAIT: $_n 仍在写入，本轮跳过，下一轮再同步"
            return 2
        fi
        _sh=$(sha256sum "$_s" 2>/dev/null | awk '{print $1}')
        [ -n "$_sh" ] || return 3
        if [ -f "$_d" ] && [ "$_sh" = "$(sha256sum "$_d" 2>/dev/null | awk '{print $1}')" ]; then
            return 1
        fi
        _st1=$(file_stamp "$_s")
        rm -f "$_tmp" 2>/dev/null
        cp -f "$_s" "$_tmp" 2>/dev/null || { rm -f "$_tmp" 2>/dev/null; return 3; }
        _st2=$(file_stamp "$_s")
        if [ "$_st1" = "$_st2" ] && \
           [ "$(sha256sum "$_tmp" 2>/dev/null | awk '{print $1}')" = "$_sh" ]; then
            mv -f "$_tmp" "$_d" 2>/dev/null || { rm -f "$_tmp" 2>/dev/null; return 3; }
            log "同步启动文件：$_n"
            return 0
        fi
        rm -f "$_tmp" 2>/dev/null
    done
    log "WAIT: $_n 复制期间仍有写入，已丢弃，下一轮重试"
    return 2
}

# 挂载点是否已挂载某设备
mnt_of_dev() { findmnt -n -o TARGET "$(real_dev "$1")" 2>/dev/null | head -n1; }

ensure_mounted_rw() {
    dev="$1"; dir="$2"
    if [ -z "$dev" ]; then return 1; fi
    # 允许直接传目录（调试 / 演练模式）
    if [ -d "$dev" ]; then echo "$dev"; return 0; fi
    existing=$(mnt_of_dev "$dev")
    if [ -n "$existing" ]; then
        if touch "$existing/.oesboot-w" 2>/dev/null; then
            rm -f "$existing/.oesboot-w" 2>/dev/null
            echo "$existing"
            return 0
        fi
        if mount -o remount,rw "$existing" 2>/dev/null && touch "$existing/.oesboot-w" 2>/dev/null; then
            rm -f "$existing/.oesboot-w" 2>/dev/null
            echo "$existing"
            return 0
        fi
        # 已挂载但写不进去（例如被 fnOS 以只读方式挂到 /vol00）：另挂一份
        mkdir -p "$dir" 2>/dev/null || return 1
        mount -o rw "$dev" "$dir" 2>/dev/null && { echo "$dir"; return 0; }
        return 1
    fi
    mkdir -p "$dir" 2>/dev/null || return 1
    if mount -t vfat -o rw,utf8=1,umask=0022 "$dev" "$dir" 2>/dev/null; then
        echo "$dir"; return 0
    fi
    if mount -o rw "$dev" "$dir" 2>/dev/null; then
        echo "$dir"; return 0
    fi
    return 1
}

# ---------------------------------------------------------------------------
# 现状探测
# ---------------------------------------------------------------------------
current_root_dev() { real_dev "$(mnt_src /)"; }

current_root_base() {
    base_of "$(partuuid_of "$(current_root_dev)")"
}

# u-boot 实际加载内核的分区（来自 /proc/cmdline 的 ubootpart=）
uboot_part() {
    p=$(sed -n 's/.*[[:space:]]ubootpart=\([^[:space:]]*\).*/\1/p' /proc/cmdline 2>/dev/null)
    [ -z "$p" ] && p=$(sed -n 's/^ubootpart=\([^[:space:]]*\).*/\1/p' /proc/cmdline 2>/dev/null)
    echo "$p"
}

emmc_boot_part() {
    for d in /sys/block/mmcblk[0-9]*; do
        [ -d "$d" ] || continue
        case "$(basename "$d")" in *boot*|*rpmb) continue ;; esac
        name=$(basename "$d")
        for p in /dev/${name}p1; do
            [ -b "$p" ] && echo "$p" && return 0
        done
    done
    return 1
}

# 解析引导分区
loader_part() {
    if [ -n "${LOADER_PART:-}" ] && dev_exists "$LOADER_PART"; then
        echo "$LOADER_PART"; return 0
    fi
    u=$(uboot_part)
    if [ -n "$u" ] && dev_exists "$u"; then
        echo "$u"; return 0
    fi
    emmc_boot_part
}

# 运行系统 /boot 所在分区
boot_part() {
    b=$(mnt_src /boot)
    [ -n "$b" ] && echo "$(real_dev "$b")"
}

# /boot 目录（注意：mnt_src 返回的是设备，这里需要挂载点）
boot_dir() {
    if [ -f /boot/fnEnv.txt ]; then echo /boot; return 0; fi
    b=$(boot_part)
    if [ -n "$b" ]; then
        d=$(mnt_of_dev "$b")
        if [ -n "$d" ]; then echo "$d"; return 0; fi
        ensure_mounted_rw "$b" "$RUN/boot" && return 0
    fi
    d=$(mnt_of_dev "$(loader_part)")
    [ -n "$d" ] && echo "$d"
}

# 读取某目录下 fnEnv.txt 的某个键
env_get() {
    f="$1"; k="$2"
    [ -f "$f" ] || return 1
    sed -n "s/^[[:space:]]*$k[[:space:]]*=[[:space:]]*//p" "$f" | head -n1
}

# 从 fnEnv.txt 里取引用文件的"纯文件名"（去掉可能存在的引号/空格）
env_fname() {
    v=$(env_get "$1" "$2")
    [ -n "$v" ] || return 1
    printf '%s' "$v" | tr -d '\r' | sed -e 's/["'\'']//g' -e 's/[[:space:]]*$//'
}

# ---------------------------------------------------------------------------
# fnEnv.txt 重写
#   $1 = 源 env 文件（模板）  $2 = 输出文件
#   $3 = 目标 root 的 PARTUUID 基址
#   $4 = 目标 boot（系统盘 p1）的 PARTUUID 基址
#   $5 = kernelfile   $6 = initrdfile   $7 = fdtfile（可空）
# ---------------------------------------------------------------------------
rewrite_env() {
    src="$1"; out="$2"; rbase="$3"; bbase="$4"; kf="$5"; inf="$6"; ff="$7"

    [ -f "$src" ] || return 1
    awk -v rbase="$rbase" -v bbase="$bbase" \
        -v kf="$kf" -v inf="$inf" -v ff="$ff" '
    function reuuid(v, nb, sfx,   r) {
        if (match(v, /[0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]-/)) {
            r = substr(v, 1, RSTART - 1) nb "-" substr(v, RSTART + 9)
            if (index(r, nb) > 0) return r
        }
        if (substr(v, 1, 9) == "PARTUUID=") return "PARTUUID=" nb sfx
        if (substr(v, 1, 1) == "/") return "PARTUUID=" nb sfx
        return nb sfx
    }
    {
        line = $0
        key = line
        sub(/[[:space:]]*=.*$/, "", key)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
        val = line
        sub(/^[^=]*=/, "", val)

        if (key == "kernelfile" && kf  != "") { print "kernelfile="  kf;  took_k = 1; next }
        if (key == "initrdfile" && inf != "") { print "initrdfile="  inf; took_i = 1; next }
        if (key == "fdtfile"    && ff  != "") { print "fdtfile="     ff;  took_f = 1; next }
        if (key == "rootdev")   { print "rootdev="   reuuid(val, rbase, "-02"); took_r = 1; next }
        if (key == "instdisk_bootuuid") { print "instdisk_bootuuid=" reuuid(val, bbase, "-01"); took_ib = 1; next }
        if (key == "instdisk_rootuuid") { print "instdisk_rootuuid=" reuuid(val, rbase, "-02"); took_ir = 1; next }
        print line
    }
    END {
        if (!took_k  && kf  != "") print "kernelfile="  kf
        if (!took_i  && inf != "") print "initrdfile="  inf
        if (!took_f  && ff  != "") print "fdtfile="     ff
        if (!took_r)               print "rootdev=PARTUUID=" rbase "-02"
        if (!took_ib)              print "instdisk_bootuuid=" bbase "-01"
        if (!took_ir)              print "instdisk_rootuuid=" rbase "-02"
    }' "$src" > "$out" || return 1

    # 基本校验：必须有内核与根设备
    grep -q '^rootdev=' "$out" || return 1
    grep -q '^kernelfile=' "$out" || return 1
    return 0
}

# ---------------------------------------------------------------------------
# 同步：把 $1（源 boot 目录）的启动文件同步到 $2（引导分区设备）
#   $3 = root base   $4 = boot base
# ---------------------------------------------------------------------------
do_sync() {
    src_dir="$1"; dst_dev="$2"; rbase="$3"; bbase="$4"

    [ -d "$src_dir" ] || die "源启动目录不存在：$src_dir"
    [ -f "$src_dir/fnEnv.txt" ] || die "源目录缺少 fnEnv.txt：$src_dir"
    [ -n "$rbase" ] || die "未获取到目标 root 的 PARTUUID"

    dst=$(ensure_mounted_rw "$dst_dev" "$RUN/loader") || die "无法挂载引导分区：$dst_dev"
    src_real=$(cd "$src_dir" 2>/dev/null && pwd)
    dst_real=$(cd "$dst" 2>/dev/null && pwd)
    same_dir=0
    [ "$src_real" = "$dst_real" ] && same_dir=1
    mounted_by_us=0
    [ "$dst" = "$RUN/loader" ] && mounted_by_us=1

    kf=$(env_fname "$src_dir/fnEnv.txt" kernelfile)
    inf=$(env_fname "$src_dir/fnEnv.txt" initrdfile)
    ff=$(env_fname "$src_dir/fnEnv.txt" fdtfile)

    # 源 env 缺失关键字段时，从 /boot 里挑最新的内核
    if [ -z "$kf" ] || [ ! -f "$src_dir/$kf" ]; then
        cand=$(ls -1t "$src_dir"/vmlinuz-* 2>/dev/null | head -n1)
        [ -n "$cand" ] || die "源启动目录里找不到可用内核（vmlinuz-*）"
        kf=$(basename "$cand")
        log "WARN: 源 fnEnv 未给出有效 kernelfile，改用 $kf"
    fi
    if [ -z "$inf" ] || [ ! -f "$src_dir/$inf" ]; then
        cand=$(ls -1t "$src_dir"/uInitrd-* "$src_dir"/initrd.img-* 2>/dev/null | head -n1)
        [ -n "$cand" ] && inf=$(basename "$cand")
    fi

    # ---- 目标内核完整性 ------------------------------------------------
    # 升级装到一半失败时，/boot 会留下残骸且 fnEnv 可能已指向它。
    # 把它搬到 eMMC 就等于把"起不来"固化下来，必须挡住。
    if ! kernel_sane "$src_dir/$kf"; then
        [ "$mounted_by_us" = "1" ] && umount "$dst" 2>/dev/null
        if [ ! -f "$RUN/kernel-bad" ]; then
            log "WARN: $kf 不像一份完整内核（大小或格式异常），拒绝同步到引导分区。引导配置保持原样，可正常启动旧系统。"
            echo "$(date '+%Y-%m-%d %H:%M:%S') $kf" > "$RUN/kernel-bad" 2>/dev/null || true
        fi
        return 0
    fi
    rm -f "$RUN/kernel-bad" 2>/dev/null

    # 格式对了还不够：内核必须连同 modules 一起装完才算一份可启动的新系统。
    # OTA 失败时常见"内核文件已完整落盘、modules 没装"，此时同步过去就升级成
    # "新内核 + 旧用户态"，和当初失联的成因一模一样，只是方向相反。
    if ! modules_ok "$kf"; then
        [ "$mounted_by_us" = "1" ] && umount "$dst" 2>/dev/null
        if [ ! -f "$RUN/kernel-bad" ]; then
            log "WARN: $kf 对应的 /lib/modules/${kf#vmlinuz-} 不存在，内核只装了一半，拒绝同步（引导保持原样，重启仍进当前系统）"
            echo "$(date '+%Y-%m-%d %H:%M:%S') $kf modules-missing" > "$RUN/kernel-bad" 2>/dev/null || true
        fi
        return 0
    fi

    # ---- 空间预检 + 精简旧内核 -------------------------------------------
    need=0
    for f in "$kf" "$inf" "$ff"; do
        [ -n "$f" ] || continue
        need=$((need + $(size_kb "$src_dir/$f")))
    done
    need=$((need + 16384))     # 预留 16MB 给 fnEnv / 临时文件
    avail=$(avail_kb "$dst")
    if [ -n "$avail" ] && [ "$avail" -lt "$need" ] 2>/dev/null; then
        log "WARN: 引导分区剩余 $((avail / 1024))MB < 需要 $((need / 1024))MB，开始精简旧内核"
        keep_k=$(env_fname "$dst/.oesboot/fnEnv.orig" kernelfile)
        keep_i=$(env_fname "$dst/.oesboot/fnEnv.orig" initrdfile)
        for old in "$dst"/vmlinuz-* "$dst"/uInitrd-*; do
            [ -f "$old" ] || continue
            b=$(basename "$old")
            [ "$b" = "$kf" ] && continue
            [ "$b" = "$inf" ] && continue
            [ -n "$keep_k" ] && [ "$b" = "$keep_k" ] && continue
            [ -n "$keep_i" ] && [ "$b" = "$keep_i" ] && continue
            log "清理旧启动文件：$b"
            rm -f "$old"
        done
        avail=$(avail_kb "$dst")
        if [ -n "$avail" ] && [ "$avail" -lt "$need" ] 2>/dev/null; then
            [ "$mounted_by_us" = "1" ] && umount "$dst" 2>/dev/null
            die "引导分区空间不足（剩余 $((avail / 1024))MB，需要 $((need / 1024))MB）"
        fi
    fi

    # ---- 首次同步前备份原始 env ------------------------------------------
    if [ ! -f "$dst/.oesboot/fnEnv.orig" ] && [ -f "$dst/fnEnv.txt" ]; then
        mkdir -p "$dst/.oesboot" 2>/dev/null
        cp -f "$dst/fnEnv.txt" "$dst/.oesboot/fnEnv.orig" 2>/dev/null \
            && log "已备份原始引导配置：.oesboot/fnEnv.orig"
    fi

    # ---- 复制启动文件 -----------------------------------------------------
    rm -f "$dst"/*.oesboot-tmp 2>/dev/null     # 清理上轮掉电残留

    pending=0
    for f in "$kf" "$inf" "$ff"; do
        [ -n "$f" ] || continue
        [ "$same_dir" = "1" ] && continue
        case "$f" in */*) continue ;; esac      # dtb 路径相对 dtb/，由目录同步负责
        [ -f "$src_dir/$f" ] || { log "WARN: 源文件缺失，跳过 $f"; continue; }
        copy_entry "$src_dir/$f" "$dst/$f" "$f"
        case $? in
            0|1) : ;;
            2)   pending=1 ;;
            *)   [ "$mounted_by_us" = "1" ] && umount "$dst" 2>/dev/null
                 die "复制校验失败：$f（已保留引导分区原文件，未改动 fnEnv）" ;;
        esac
    done

    # 内核/initrd 还没落稳时，绝不能把 fnEnv 指向新文件名——那会让 u-boot
    # 找不到内核。本轮整体作废，保留上一份可用配置，20 秒后再来。
    if [ "$pending" = "1" ]; then
        sync
        [ "$mounted_by_us" = "1" ] && umount "$dst" 2>/dev/null
        return 0
    fi

    # 设备树目录整体同步（体积小，避免 fdtfile 与实际 dtb 不匹配）
    if [ -d "$src_dir/dtb" ] && [ -n "$ff" ] && [ "$same_dir" != "1" ]; then
        if need_cmd rsync; then
            rsync -a --delete "$src_dir/dtb/" "$dst/dtb/" 2>/dev/null || log "WARN: dtb 同步失败"
        else
            rm -rf "$dst/dtb.new" 2>/dev/null
            cp -a "$src_dir/dtb" "$dst/dtb.new" 2>/dev/null && \
                rm -rf "$dst/dtb" && mv "$dst/dtb.new" "$dst/dtb"
        fi
    fi

    # ---- 生成并落盘 fnEnv.txt（原子替换）---------------------------------
    # 目标已有 fnEnv 时以它为模板：1.2.07xx 起 /boot/fnEnv.txt 不再含 rootdev，
    # 用它当模板会丢字段；同时也能保留 instdisk_* 的原始写法。
    tmpl="$src_dir/fnEnv.txt"
    if [ -f "$dst/fnEnv.txt" ]; then
        tmpl="$dst/fnEnv.txt"
    fi
    if ! rewrite_env "$tmpl" "$RUN/fnEnv.new" "$rbase" "$bbase" "$kf" "$inf" "$ff"; then
        [ "$mounted_by_us" = "1" ] && umount "$dst" 2>/dev/null
        die "生成 fnEnv.txt 失败"
    fi

    if [ -f "$dst/fnEnv.txt" ] && cmp -s "$RUN/fnEnv.new" "$dst/fnEnv.txt"; then
        changed=0
    else
        changed=1
        cp -f "$RUN/fnEnv.new" "$dst/fnEnv.txt.tmp" || { [ "$mounted_by_us" = "1" ] && umount "$dst" 2>/dev/null; die "写入 fnEnv.txt 失败"; }
        mv -f "$dst/fnEnv.txt.tmp" "$dst/fnEnv.txt" || { [ "$mounted_by_us" = "1" ] && umount "$dst" 2>/dev/null; die "替换 fnEnv.txt 失败"; }
    fi

    mkdir -p "$dst/.oesboot" 2>/dev/null
    {
        echo "time=$(date '+%Y-%m-%d %H:%M:%S')"
        echo "root_base=$rbase"
        echo "boot_base=$bbase"
        echo "kernelfile=$kf"
        echo "initrdfile=$inf"
        echo "fdtfile=$ff"
        echo "loader=$dst_dev"
        echo "source=$src_dir"
        echo "reason=$SYNC_REASON"
        echo "changed=$changed"
    } > "$dst/.oesboot/last-sync.tmp" 2>/dev/null && mv -f "$dst/.oesboot/last-sync.tmp" "$dst/.oesboot/last-sync" 2>/dev/null

    # 关机/重启前的那一次单独留档：开机后可以确认"重启前确实同步过"
    if [ "$SYNC_REASON" = "shutdown" ]; then
        cp -f "$dst/.oesboot/last-sync" "$dst/.oesboot/last-shutdown" 2>/dev/null || true
    fi

    # 监视计数单独存：日志静默之后，"它到底跑了几次"仍然要能看到
    if [ "$SYNC_REASON" = "watch" ]; then
        _hf="$RUN/watch.hits"
        _hn=$(cat "$_hf" 2>/dev/null || echo 0)
        case "$_hn" in ''|*[!0-9]*) _hn=0 ;; esac
        echo $((_hn + 1)) > "$_hf" 2>/dev/null || true
    fi

    sync
    [ "$mounted_by_us" = "1" ] && umount "$dst" 2>/dev/null
    SYNC_DONE=1

    if [ "${changed:-1}" = "0" ]; then
        # 定时巡检 / 升级监视没变化就不写日志，避免每 20 秒刷屏；
        # 手动、钩子、关机、开机触发则留痕
        case "$SYNC_REASON" in
            timer|watch) return 0 ;;
        esac
        log "检查通过：引导分区已一致（rootdev=$rbase-02, kernel=$kf, 触发=$SYNC_REASON）"
    else
        log "OK: 引导分区已同步（rootdev=$rbase-02, kernel=$kf, 触发=$SYNC_REASON）"
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 动作：sync
# ---------------------------------------------------------------------------
do_sync_action() {
    force_root=""; force_boot=""; force_src=""; force_dst=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --root-base) force_root="$2"; shift 2 ;;
            --boot-base) force_boot="$2"; shift 2 ;;
            --source)    force_src="$2"; shift 2 ;;
            --dest)      force_dst="$2"; shift 2 ;;
            --reason)    SYNC_REASON="$2"; shift 2 ;;
            *) shift ;;
        esac
    done

    root_dev=$(current_root_dev)
    rbase=$(base_of "$(partuuid_of "$root_dev")")
    [ -n "$rbase" ] || rbase="$force_root"
    [ -n "$rbase" ] || die "当前根分区没有 PARTUUID，无法同步（非 OES 引导结构？）"

    # 系统升级安装期间让路：OTA 自己会操作引导分区，周期任务此时插进去
    # 只会互相打断。判据是"正在发生的事"，不是"窗口开没开"。
    case "$SYNC_REASON" in
        watch|timer)
            # 两个零风险信号：/boot 的内核文件刚变过、或包管理器在跑。
            # 都没有就照常巡检（changed=0 时不写引导分区）。
            if boot_changing; then
                ota_state_write yield
                ota_yield_touch
                return 0
            fi
            if ota_busy; then
                if ota_hold_mark; then
                    ota_state_write yield
                    ota_yield_touch
                    return 0
                fi
            fi
            ota_hold_clear
            ;;
        hook)
            # 窗口内 dpkg 钩子也不写：此刻引导分区归升级程序独占，
            # 多写一次就是在替它制造失败。关机/开机仍有兜底。
            if in_upgrade_window; then
                ota_state_write yield
                log "HOLD: 系统升级进行中，钩子跳过同步（引导分区交给升级程序）"
                return 0
            fi
            ;;
    esac

    # 策略校验：锁定目标与当前运行系统不一致时，绝不按当前系统改写引导
    if [ -z "$force_root" ] && [ "${LOCK:-follow}" != "follow" ]; then
        tb=$(target_base_by_policy)
        if [ -n "$tb" ] && [ "$tb" != "$rbase" ]; then
            log "WARN: 当前运行在 $rbase-02，引导策略锁定 $tb-02，本次不做同步（等待 repair 或切换）"
            return 0
        fi
    fi

    lp="${force_dst:-$(loader_part)}"
    [ -n "$lp" ] || die "未找到引导分区（mmcblk?p1）"
    lpb=$(base_of "$(partuuid_of "$lp")")

    _src_tmp=""
    if [ -n "$force_src" ]; then
        src_dir="$force_src"
    else
        src_dir=$(boot_dir)
        # 关机/重启阶段 /boot 可能已被卸载：直接按 PARTUUID 挂系统盘 p1 当来源
        if [ ! -f "$src_dir/fnEnv.txt" ]; then
            _rbase=$(base_of "$(partuuid_of "$root_dev")")
            _bp2=$(dev_by_partuuid "${_rbase}-01")
            if [ -n "$_bp2" ]; then
                if _d2=$(ensure_mounted_rw "$_bp2" "$RUN/boot2"); then
                    if [ -f "$_d2/fnEnv.txt" ]; then
                        src_dir="$_d2"
                        _src_tmp="$_d2"
                        log "WARN: /boot 未挂载，改用 $_bp2 作为同步来源"
                    fi
                fi
            fi
        fi
    fi
    [ -d "$src_dir" ] || die "无法定位 /boot"

    if [ -z "$force_src" ] && [ "$SYNC_REASON" != "watch" ] && [ "$SYNC_REASON" != "timer" ]; then
        bp=$(boot_part)
        if [ -n "$bp" ] && [ "$(real_dev "$bp")" != "$(real_dev "$lp")" ]; then
            log "同步来源：/boot（$bp）→ 引导分区 $lp"
        fi
    fi

    # instdisk_* 记录的是"系统盘"（rootdev 所在盘），不是 u-boot 读取启动文件的盘，
    # 因此默认跟随 root base；只有显式 --boot-base 才覆盖（切换/迁移场景）。
    tgt_base="${force_root:-$rbase}"
    tgt_boot_base="${force_boot:-$tgt_base}"
    [ -z "$tgt_boot_base" ] && tgt_boot_base="$tgt_base"

    do_sync "$src_dir" "$lp" "$tgt_base" "$tgt_boot_base"
    [ -n "$_src_tmp" ] && umount "$_src_tmp" 2>/dev/null

    if [ "$tgt_base" != "$rbase" ]; then
        log "WARN: 引导已指向 $tgt_base-02，但当前仍运行在 $rbase-02，需重启生效"
    fi
}

# ---------------------------------------------------------------------------
# 混合引导布局：让 /boot 直接挂到 u-boot 真正读取的 eMMC 引导分区。
#
# 根因（论坛实证）：OES 的 u-boot 永远从 eMMC p1 读内核/fnEnv；迁移 SSD 后
# /boot 还挂在 SSD p1，OTA 只更新 SSD 的 /boot，新内核进不了 u-boot 读的分区，
# 于是「安装阶段失败 / 内核版本升级提示一直存在」。修复 = 让 /boot 挂到 eMMC p1，
# OTA 写新内核时直接落进 u-boot 读取的分区。
# 本命令四步，全部可回滚：备份 → 复制启动文件 → 重写 fnEnv → 改 fstab。
# ---------------------------------------------------------------------------
HYBRID_STATE="$OTA_PERSIST/hybrid-state.txt"

hybrid() {
    _rb=$(current_root_base)
    _lp=$(loader_part)
    _bp=$(boot_part)
    [ -n "$_rb" ] || { echo "FAIL: 取不到当前根分区 PARTUUID"; return 1; }
    [ -n "$_lp" ] || { echo "FAIL: 找不到 eMMC 引导分区"; return 1; }
    _lb=$(base_of "$(partuuid_of "$_lp")")
    [ -n "$_lb" ] || { echo "FAIL: eMMC 引导分区无 PARTUUID"; return 1; }

    # 已是目标布局则跳过（幂等）
    if [ -n "$_bp" ] && [ "$(real_dev "$_bp")" = "$(real_dev "$_lp")" ]; then
        echo "SKIP: /boot 已挂在 eMMC 引导分区 $_lp，无需修改"
        return 0
    fi

    _bd=$(boot_dir)
    [ -d "$_bd" ] || { echo "FAIL: 定位不到 /boot"; return 1; }
    [ -f "$_bd/fnEnv.txt" ] || { echo "FAIL: $_bd 里没有 fnEnv.txt"; return 1; }

    mkdir -p "$OTA_PERSIST/backup" || { echo "FAIL: 无法创建备份目录"; return 1; }
    _bak="$OTA_PERSIST/backup"
    _ts=$(date +%Y%m%d-%H%M%S)

    # ---- 备份（原 fstab + 原 SSD fnEnv + 原 eMMC fnEnv）----
    cp -a /etc/fstab "$_bak/fstab.orig.$_ts" 2>/dev/null
    cp -a /etc/fstab "$_bak/fstab.orig" 2>/dev/null
    cp -a "$_bd/fnEnv.txt" "$_bak/boot-fnEnv.orig" 2>/dev/null

    # ---- 挂载 eMMC boot（已挂到 /vol00 就复用）----
    _ld=$(ensure_mounted_rw "$_lp" "$RUN/loader-hybrid") || { echo "FAIL: 无法挂载 eMMC 引导分区 $_lp"; return 1; }
    echo "挂载 eMMC 引导分区 → $_ld"
    cp -a "$_ld/fnEnv.txt" "$_bak/loader-fnEnv.orig" 2>/dev/null

    # ---- 0) 预检：装不下就别动 fstab，免得改完起不来 ----
    _need=$(du -sk "$_bd" 2>/dev/null | awk '{print $1}')
    _have=$(df -P -k "$_ld" 2>/dev/null | awk 'NR==2{print $4}')
    _need=${_need:-0}; _have=${_have:-0}
    echo "预检：/boot 需 $_need KB，eMMC 引导分区可用 $_have KB"
    if [ "$_have" -lt $((_need + 20480)) ] 2>/dev/null; then
        echo "FAIL: eMMC 引导分区空间不足（需 $((_need + 20480)) KB，可用 $_have KB），未做任何修改"
        [ "$_ld" = "$RUN/loader-hybrid" ] && umount "$_ld" 2>/dev/null
        return 1
    fi
    _ftype=$(blkid -s TYPE -o value "$_lp" 2>/dev/null)
    echo "预检：eMMC 引导分区文件系统 = ${_ftype:-未知}（FAT16 亦可，本目录文件数远低于上限）"

    # ---- 1) 把当前 /boot 全部启动文件复制到 eMMC boot ----
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --no-perms --no-owner --no-group --exclude '.oesboot' "$_bd/" "$_ld/" 2>/dev/null
    else
        cp -a "$_bd"/. "$_ld"/ 2>/dev/null
    fi
    sync
    echo "已把当前 /boot 启动文件复制到 eMMC 引导分区"

    # ---- 2) 重写 eMMC boot 的 fnEnv：rootdev 指向 SSD，instdisk_bootuuid 指向 eMMC boot ----
    kf=$(env_fname "$_bd/fnEnv.txt" kernelfile)
    inf=$(env_fname "$_bd/fnEnv.txt" initrdfile)
    ff=$(env_fname "$_bd/fnEnv.txt" fdtfile)
    tmpl="$_bd/fnEnv.txt"
    [ -f "$_ld/fnEnv.txt" ] && tmpl="$_ld/fnEnv.txt"
    rewrite_env "$tmpl" "$RUN/fnEnv.hybrid" "$_rb" "$_lb" "$kf" "$inf" "$ff" \
        || { echo "FAIL: 生成 fnEnv.txt 失败"; return 1; }
    cp -f "$RUN/fnEnv.hybrid" "$_ld/fnEnv.txt" || { echo "FAIL: 写 fnEnv.txt 失败"; return 1; }
    sync
    echo "已重写 eMMC boot/fnEnv.txt：rootdev=$_rb-02，instdisk_bootuuid=$_lb-01"

    # ---- 3) 改 fstab：/boot 指向 eMMC boot，并屏蔽它的 RemovableDisk 挂载 ----
    _fstab_new=$(awk -v lb="$_lb" '
    function is_loader(src) { return (src ~ /mmcblk/ || index(src, lb) > 0) }
    {
        line = $0
        if (line ~ /^[[:space:]]*#/ || line ~ /^[[:space:]]*$/) { print line; next }
        n = split(line, f, /[[:space:]]+/)
        if (n < 2) { print line; next }
        src = f[1]; mnt = f[2]
        if (mnt == "/boot") {
            printf "PARTUUID=%s-01 /boot", lb
            for (i = 3; i <= n; i++) printf " %s", f[i]
            printf "\n"
            boot_seen = 1
            next
        }
        if (is_loader(src) && mnt ~ /RemovableDisk|BOOT_EMMC|EMMC|emmc/) {
            print "# [oesboot] 该分区已改挂到 /boot：" line
            next
        }
        print line
    }
    END {
        if (!boot_seen) {
            print "# [oesboot] 自动添加：/boot 挂载到 u-boot 读取的 eMMC 引导分区"
            print "PARTUUID=" lb "-01 /boot vfat defaults 0 0"
        }
    }' /etc/fstab) || { echo "FAIL: 解析 /etc/fstab 失败"; return 1; }

    if [ -n "$_fstab_new" ]; then
        cp -a /etc/fstab "$_bak/fstab.mod.$_ts" 2>/dev/null
        printf '%s\n' "$_fstab_new" > /etc/fstab || { echo "FAIL: 写 /etc/fstab 失败"; return 1; }
        # 校验：fstab 语法错 = 起不来。--fake 只解析不真挂。
        if command -v mount >/dev/null 2>&1; then
            if ! mount -a --fake 2>"$RUN/fstab.check"; then
                _e=$(tail -n 2 "$RUN/fstab.check" 2>/dev/null | tr '\n' ' ')
                cp -a "$_bak/fstab.orig" /etc/fstab 2>/dev/null
                echo "FAIL: 新 fstab 校验不通过（${_e:-未知错误}），已自动还原"
                [ "$_ld" = "$RUN/loader-hybrid" ] && umount "$_ld" 2>/dev/null
                return 1
            fi
        fi
        echo "已更新 /etc/fstab：/boot 指向 eMMC 引导分区（语法校验通过）"
    fi

    # ---- 4) 落地校验：fnEnv 指向的内核文件必须真实存在于 eMMC 引导分区 ----
    _kf2=$(env_fname "$_ld/fnEnv.txt" kernelfile)
    _ff2=$(env_fname "$_ld/fnEnv.txt" fdtfile)
    if [ -n "$_kf2" ] && [ ! -f "$_ld/$_kf2" ]; then
        echo "FAIL: 引导分区里找不到 kernelfile=$_kf2，引导会失败，已停止"
        return 1
    fi
    if [ -n "$_ff2" ] && [ ! -f "$_ld/$_ff2" ]; then
        echo "WARN: 引导分区里找不到 fdtfile=$_ff2（若原来就不在，可忽略）"
    fi
    echo "落地校验通过：kernelfile=${_kf2:-未设置}$( [ -n "$_ff2" ] && echo "，fdtfile=$_ff2" )"

    # ---- 记录状态（回滚依据）----
    {
        echo "root_base=$_rb"
        echo "loader_part=$_lp"
        echo "loader_base=$_lb"
        echo "old_boot_part=${_bp:-?}"
        echo "kernelfile=$kf"
        echo "time=$(date '+%Y-%m-%d %H:%M:%S')"
    } > "$HYBRID_STATE.tmp" 2>/dev/null && mv -f "$HYBRID_STATE.tmp" "$HYBRID_STATE" 2>/dev/null

    [ "$_ld" = "$RUN/loader-hybrid" ] && umount "$_ld" 2>/dev/null
    echo "OK: 混合引导布局已就绪，重启后生效（/boot 将挂到 eMMC 引导分区）"
    return 0
}

hybrid_rollback() {
    _bak="$OTA_PERSIST/backup"
    _lp=$(loader_part)
    [ -n "$_lp" ] || { echo "FAIL: 找不到 eMMC 引导分区"; return 1; }

    _ld=$(ensure_mounted_rw "$_lp" "$RUN/loader-rb") || { echo "FAIL: 挂载 eMMC 引导分区失败"; return 1; }
    if [ -f "$_bak/loader-fnEnv.orig" ]; then
        cp -f "$_bak/loader-fnEnv.orig" "$_ld/fnEnv.txt" && echo "已恢复 eMMC boot/fnEnv.txt"
    else
        echo "WARN: 没有 eMMC fnEnv 备份，跳过"
    fi

    _f=$(ls -1t "$_bak"/fstab.orig* 2>/dev/null | head -n1)
    if [ -n "$_f" ]; then
        cp -a "$_f" /etc/fstab && echo "已恢复 /etc/fstab"
    else
        echo "WARN: 没有 fstab 备份，跳过"
    fi

    [ "$_ld" = "$RUN/loader-rb" ] && umount "$_ld" 2>/dev/null
    rm -f "$HYBRID_STATE" 2>/dev/null
    echo "OK: 回滚完成，重启后恢复原布局"
    return 0
}

# 按策略找出目标系统盘的 PARTUUID 基址（扫 blkid）
target_base_by_policy() {
    if [ -n "${TARGET_BASE:-}" ]; then
        if [ -n "$(dev_by_partuuid "$TARGET_BASE-02")" ]; then
            echo "$TARGET_BASE"; return 0
        fi
    fi
    case "${LOCK:-follow}" in
        ssd)
            for d in $(blkid -o device 2>/dev/null); do
                disk=$(pkname "$d")
                [ -n "$disk" ] || continue
                is_emmc_disk "$disk" && continue
                pu=$(partuuid_of "$d")
                [ -n "$pu" ] || continue
                b=$(base_of "$pu")
                case "$pu" in *-02) echo "$b"; return 0 ;; esac
            done
            ;;
        emmc)
            for d in $(blkid -o device 2>/dev/null); do
                disk=$(pkname "$d")
                is_emmc_disk "$disk" || continue
                pu=$(partuuid_of "$d")
                case "$pu" in *-02) echo "$(base_of "$pu")"; return 0 ;; esac
            done
            ;;
    esac
    return 0
}

# ---------------------------------------------------------------------------
# 动作：repair（当前从 eMMC 启动，但策略要求 SSD）
# ---------------------------------------------------------------------------
do_repair_action() {
    [ "${LOCK:-follow}" = "ssd" ] || { log "repair 跳过：策略不是 ssd（当前 $LOCK）"; return 0; }

    cur_disk=$(pkname "$(current_root_dev)")
    lp=$(loader_part); [ -n "$lp" ] || { log "repair 跳过：找不到引导分区"; return 0; }
    lp_disk=$(pkname "$lp")

    # 只有"运行在 eMMC、引导分区也在 eMMC"才需要 repair
    is_emmc_disk "${cur_disk:-none}" || { log "repair 跳过：当前未运行在 eMMC"; return 0; }
    [ "${cur_disk:-x}" = "${lp_disk:-y}" ] || { log "repair 跳过：引导分区非 eMMC"; return 0; }

    want=$(target_base_by_policy)
    [ -n "$want" ] || { log "repair 跳过：未找到可用的 SSD 系统分区"; return 0; }
    [ "$want" = "$(base_of "$(partuuid_of "$(current_root_dev)")")" ] && { log "repair 跳过：当前已在 SSD"; return 0; }

    ssd_boot=$(dev_by_partuuid "$want-01")
    [ -n "$ssd_boot" ] || { log "WARN: 未找到 SSD 启动分区 $want-01"; return 0; }

    src=$(ensure_mounted_rw "$ssd_boot" "$RUN/ssd") || { log "WARN: 无法挂载 SSD 启动分区"; return 0; }
    by_us=0; [ "$src" = "$RUN/ssd" ] && by_us=1

    if [ ! -f "$src/fnEnv.txt" ]; then
        [ "$by_us" = "1" ] && umount "$src" 2>/dev/null
        log "WARN: SSD 启动分区缺少 fnEnv.txt，放弃自动修复"
        return 0
    fi

    log "开始自动修复：用 SSD 启动分区反向同步 eMMC 引导分区"
    do_sync "$src" "$lp" "$want" "$want" || { [ "$by_us" = "1" ] && umount "$src" 2>/dev/null; return 1; }
    [ "$by_us" = "1" ] && umount "$src" 2>/dev/null

    log "OK: 已把引导切回 SSD（$want-02）"

    if [ "${AUTO_REBOOT:-0}" = "1" ]; then
        n=$(cat "$RUN/repair.count" 2>/dev/null || echo 0)
        n=$((n + 1)); echo "$n" > "$RUN/repair.count"
        if [ "$n" -le 2 ]; then
            log "自动重启以进入 SSD 系统（第 $n 次）"
            (sleep 3; reboot) >/dev/null 2>&1 &
        else
            log "WARN: 连续自动修复超过 2 次，停止自动重启，请人工检查"
        fi
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 动作：status
# ---------------------------------------------------------------------------
do_status() {
    root_dev=$(current_root_dev)
    rbase=$(base_of "$(partuuid_of "$root_dev")")
    lp=$(loader_part)
    lpb=$(base_of "$(partuuid_of "$lp")")
    bp=$(real_dev "$(boot_part)")
    bs=$(boot_dir)
    echo "version=$VERSION"
    echo "enabled=$ENABLED"
    echo "lock=${LOCK:-follow}"
    echo "auto_repair=${AUTO_REPAIR:-1}"
    echo "auto_reboot=${AUTO_REBOOT:-0}"
    echo "root_dev=$root_dev"
    echo "root_base=$rbase"
    echo "root_disk=$(pkname "$root_dev")"
    echo "loader_part=$lp"
    echo "loader_base=$lpb"
    echo "boot_part=$bp"
    echo "ubootpart=$(uboot_part)"
    echo "running_kernel=$(uname -r)"
    if [ -n "$bs" ] && [ -f "$bs/fnEnv.txt" ]; then
        echo "boot_kernel=$(env_fname "$bs/fnEnv.txt" kernelfile)"
        echo "boot_env_root=$(env_get "$bs/fnEnv.txt" rootdev)"
        if [ -f "$bs/.oesboot/last-sync" ]; then
            sed -n 's/^/last_sync_/p' "$bs/.oesboot/last-sync" 2>/dev/null
        fi
    fi
    if [ -n "$lp" ]; then
        # 只复用已有挂载点：升级窗口内主动 mount 会打断升级程序
        d=$(mnt_of_dev "$lp")
        if [ -z "$d" ] && ! in_upgrade_window; then
            d="$RUN/loader-ro"; mkdir -p "$d"
            mount -o ro "$lp" "$d" 2>/dev/null && { mnt_tmp="$d"; }
        fi
        if [ -n "$d" ] && [ -f "$d/fnEnv.txt" ]; then
            echo "loader_kernel=$(env_fname "$d/fnEnv.txt" kernelfile)"
            echo "loader_env_root=$(env_get "$d/fnEnv.txt" rootdev)"
        fi
        [ -n "${mnt_tmp:-}" ] && umount "$mnt_tmp" 2>/dev/null
    fi

    # 升级窗口状态：让 UI 能区分"守护在跑"和"守护主动让路"
    if in_upgrade_window; then
        echo "upgrade_window=1"
        echo "upgrade_since=$(head -n1 "$UPGRADE_FLAG" 2>/dev/null | tr -d '\n')"
        echo "upgrade_flag_age=$(file_age "$UPGRADE_FLAG")"
    else
        echo "upgrade_window=0"
    fi
    if [ -f "$OTA_STATE" ]; then
        sed -n 's/^/ota_/p' "$OTA_STATE" 2>/dev/null
    fi
    # 上一轮升级的结算结果（持久化过，重启后仍可读）
    if [ -f "$OTA_LAST" ]; then
        sed -n 's/^/last_/p' "$OTA_LAST" 2>/dev/null
    fi
    [ -f "$OTA_REPORT" ] && echo "ota_report=$OTA_REPORT"
    [ -f "$OTA_TRACE" ] && echo "ota_trace=$OTA_TRACE"
}

# ---------------------------------------------------------------------------
main() {
    action="${1:-sync}"
    [ $# -gt 0 ] && shift

    # 这里就要认出触发来源：升级窗口的早退判断发生在 do_sync_action 之前，
    # 等到那里再解析 --reason 就晚了（会一直当成 manual，窗口优化形同虚设）。
    _prev=""
    for _a in "$@"; do
        [ "$_prev" = "--reason" ] && SYNC_REASON="$_a"
        _prev="$_a"
    done

    case "$action" in
        status)
            do_status
            exit 0
            ;;
        rewrite)   # 调试用：rewrite <src> <out> <rootbase> <bootbase> <k> <i> <f>
            rewrite_env "$@"
            exit $?
            ;;
        bundle)    # 手动抓现场：窗口开着也能抓，不依赖结算
            ota_bundle
            echo "$OTA_REPORT"
            exit 0
            ;;
        hybrid)    # 混合引导布局：让 /boot 挂到 eMMC 引导分区（修复 OTA 安装失败）
            hybrid
            exit $?
            ;;
        hybrid-rollback)
            hybrid_rollback
            exit $?
            ;;
        sync|repair|"")
            ;;
        *)
            echo "usage: $0 {sync|repair|status|bundle|hybrid|hybrid-rollback}" >&2
            exit 1
            ;;
    esac

    # 守护开关只管「自动触发」，管不到用户亲手点的那一下。
    # 1.1.15 及以前这里是无条件 exit 0，于是「立即同步引导」在守护关闭时
    # 只打印一行 disabled，界面却显示"同步完成"——eMMC 一个字节都没动。
    if [ "${ENABLED:-1}" != "1" ]; then
        case "${SYNC_REASON:-manual}" in
            manual|"") ;;
            *)
                echo "oesboot guard disabled（守护已关闭，自动同步不触发；手动同步请用 --reason manual）"
                echo "RESULT=skip:disabled"
                exit 0
                ;;
        esac
    fi

    # 升级窗口内是否要让路，取决于"现在是不是真的在升级"，而不是"窗口开没开"。
    # 点「启动升级守护」只是打开监视，用户可能十分钟后才去点升级；
    # 这十分钟里若一律 exit 0，就等于"开了守护反而一次都不同步"，
    # 界面还会把每次巡检记成一次让路。
    # 判据只看两条零风险的信号：/boot 的内核文件 120 秒内变过，或包管理器在跑。
    case "${SYNC_REASON:-}" in
        watch|timer)
            if [ "$action" = "sync" ] && in_upgrade_window; then
                if boot_changing || ota_busy; then
                    ota_state_write yield
                    ota_yield_touch
                    ota_trace
                    ota_file_trace
                    ota_probe
                    exit 0
                fi
                # 没有升级活动：继续往下走常规巡检。changed=0 时不会写引导分区。
                ota_state_write
            fi
            ;;
    esac

    # 窗口已经关了（用户点了结束，或者系统升级完自动重启把 /run 清空）：
    # 结算这一轮到底成没成，失败就把现场打包留证。幂等，只做一次。
    case "${SYNC_REASON:-}" in
        watch|timer|boot)
            [ "$action" = "sync" ] && ota_finalize
            ;;
    esac

    # 互斥：避免关机同步与定时任务并发写同一分区。
    # 关机/开机这两个只有一次的时机必须等到锁，不能因为升级监视正在跑就放弃。
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$LOCKFILE"
        case "$action" in
            sync)
                case "$SYNC_REASON" in
                    shutdown|boot) flock -w 25 9 || { log "已有同步任务在运行，等待 25s 未拿到锁，跳过"; exit 0; } ;;
                    *)             flock -n 9 || { log "已有同步任务在运行，跳过"; exit 0; } ;;
                esac
                ;;
            *) flock -n 9 || { log "已有同步任务在运行，跳过"; exit 0; } ;;
        esac
    fi

    SYNC_DONE=0
    case "$action" in
        sync)   do_sync_action "$@" ;;
        repair) do_repair_action ;;
    esac

    # 给调用方（管家 / systemd）一个明确结论：脚本返回 0 不等于"同步成功"，
    # 让路、跳过、拒绝同步也都是 0。以前上层只看返回值，于是把
    # "什么都没做"当成了"同步完成"。
    case "${SYNC_REASON:-}" in
        watch|timer) ;;
        *)
            if [ "$action" = "sync" ] || [ -z "$action" ]; then
                if [ "${SYNC_DONE:-0}" = "1" ]; then
                    echo "RESULT=done"
                else
                    echo "RESULT=skip:noop"
                fi
            fi
            ;;
    esac
}

main "$@"
