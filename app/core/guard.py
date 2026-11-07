"""升级守护的部署：把纯 shell 的引导同步脚本 + systemd 单元装进两个系统的 rootfs。

为什么两边都要装：设备可能因为引导被改而启动到 eMMC（旧系统），
那个系统里没有任何应用记录，但 systemd 单元照样会执行——它就是我们
"自动把引导切回 SSD"的最后一环。
"""
import os
import shutil

from .util import Mount, append_file, read_file, run, write_file

SCRIPT_NAME = "oes-boot-sync"
SCRIPT_DST = "/usr/local/sbin/" + SCRIPT_NAME
CONF_DST = "/etc/oesboot.conf"
UNIT_DIR = "/etc/systemd/system"
DPKG_HOOK_DST = "/etc/dpkg/dpkg.cfg.d/99oesboot-sync"
DPKG_WRAPPER_DST = "/usr/local/sbin/oesboot-dpkg-hook"
# 升级监视开关（tmpfs，重启即消失 → 监视自动结束，不会长期常驻轮询）
UPGRADE_FLAG = "/run/oesboot-upgrade"
UPGRADE_STATE = "/var/lib/oesboot/upgrade.state"

UNIT_SYNC = """[Unit]
Description=OES boot partition guard (sync kernel/initrd/fnEnv)
DefaultDependencies=no
After=local-fs.target
Before=shutdown.target umount.target
ConditionPathExists={script}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=-{script} repair
ExecStart={script} sync --reason boot
TimeoutStartSec=180
Nice=10

[Install]
WantedBy=multi-user.target
""".format(script=SCRIPT_DST)

UNIT_SHUTDOWN = """[Unit]
Description=OES boot partition guard (sync before shutdown)
DefaultDependencies=no
# 注意：这里绝不能写 After=local-fs.target。
# 关机路径上 After 表示"等对方停掉之后再执行"，那会把 /boot 一起卸掉，
# 同步就失去了来源。只要早于 umount.target 就能拿到挂载着的 /boot。
Before=shutdown.target umount.target
ConditionPathExists={script}

[Service]
Type=oneshot
ExecStart={script} sync --reason shutdown
# OTA 重启会直接进 shutdown.target，这里必须跑得完且不被超时打断
TimeoutStartSec=120
Nice=10

[Install]
WantedBy=shutdown.target
""".format(script=SCRIPT_DST)

UNIT_CHECK = """[Unit]
Description=OES boot partition guard (periodic check)
After=local-fs.target
ConditionPathExists={script}

[Service]
Type=oneshot
ExecStart={script} sync --reason timer
Nice=19
IOSchedulingClass=idle
""".format(script=SCRIPT_DST)

UNIT_TIMER = """[Unit]
Description=Periodic OES boot partition check

[Timer]
OnBootSec=5min
OnUnitActiveSec=10min
AccuracySec=30s

[Install]
WantedBy=timers.target
"""

# 升级监视：只在 /run/oesboot-upgrade 存在时工作（用户点"开始升级守护"后创建）。
# 20 秒一次，OTA 把新内核写进 /boot 后最迟 20 秒就会同步到 eMMC 引导分区，
# 远早于系统自动重启，因此不需要任何人工介入。
UNIT_WATCH = """[Unit]
Description=OES boot partition guard (upgrade watch)
After=local-fs.target
ConditionPathExists={flag}

[Service]
Type=oneshot
ExecStartPre=-/bin/sh -c 'find {flag} -mmin +360 -delete 2>/dev/null || true'
ExecStart={script} sync --reason watch
TimeoutStartSec=120
Nice=15
""".format(flag=UPGRADE_FLAG, script=SCRIPT_DST)

UNIT_WATCH_TIMER = """[Unit]
Description=Fast boot-partition check while a system upgrade is running

[Timer]
OnBootSec=15s
OnUnitActiveSec=20s
AccuracySec=3s

[Install]
WantedBy=timers.target
"""

APT_HOOK = """// 由 OES 系统盘管家维护：内核包安装完成后立即同步引导分区
DPkg::Post-Invoke { "/usr/local/sbin/oesboot-dpkg-hook"; };
"""

# dpkg 自己的钩子：fnOS 的更新器若直接 `dpkg -i`，不读 apt.conf.d，只认这个
#
# 1.1.14 血泪教训：这里原来写的是
#     post-invoke "/usr/local/sbin/oes-boot-sync sync >/dev/null 2>&1 || true";
# dpkg 的配置解析器对引号内特殊字符的处理和 shell 不同，一旦它认为引号不配平，
# 就会在**每一次 dpkg 操作前**直接退出：
#     dpkg: error: configuration error: .../99oesboot-sync:1: unbalanced quotes
# 结果是整台机器的包管理彻底瘫痪——OTA 装内核 4 秒就"失败"，内核压根没解包，
# 看上去和引导布局、守护让路毫无关系的怪现象，全是这一个文件造成的。
# 现在改成不带任何引号和元字符的调用形式，把逻辑收进 wrapper 脚本里。
DPKG_HOOK = """post-invoke /usr/local/sbin/oesboot-dpkg-hook
"""

# wrapper 只做一件事：同步，且无论如何 exit 0。
# dpkg 的 post-invoke 返回值会污染退出码，绝不能让它影响装包结果。
DPKG_WRAPPER = """#!/bin/sh
# 由 OES 系统盘管家维护：dpkg 装完包后同步引导分区。
# 这里必须永远 exit 0 —— 任何非零都会让升级器误判"安装失败"。
/usr/local/sbin/oes-boot-sync sync >/dev/null 2>&1 || true
exit 0
"""

CONF_TMPL = """# OES 系统盘管家 —— 升级守护配置
# 由 Web 控制台维护，手工修改后请重启应用或执行 oesboot sync
ENABLED={enabled}
# follow: 引导跟随当前运行系统；ssd/emmc: 锁定启动盘
LOCK={lock}
# 启动到 eMMC 且锁定 SSD 时，自动用 SSD 启动分区修复 eMMC 引导
AUTO_REPAIR={auto_repair}
# 修复成功后自动重启进入 SSD（0=不自动重启）
AUTO_REBOOT={auto_reboot}
# 引导分区（留空=自动探测 ubootpart / mmcblk?p1）
LOADER_PART={loader_part}
KEEP_PREV=1
"""


def read_conf():
    conf = {
        "ENABLED": "1",
        "LOCK": "follow",
        "AUTO_REPAIR": "1",
        "AUTO_REBOOT": "0",
        "LOADER_PART": "",
    }
    text = read_file(CONF_DST)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        conf[k.strip()] = v.strip().strip('"')
    return conf


def write_conf(conf, both=False):
    """写入当前 rootfs 的配置；both=True 时同步写入备用系统的 rootfs。"""
    text = render_conf(conf)
    write_file(CONF_DST, text, 0o644)
    if not both:
        return
    for part in other_rootfs():
        try:
            with Mount(part, readonly=False, prefix="oesconf") as mnt:
                write_file(os.path.join(mnt, CONF_DST.lstrip("/")), text, 0o644)
        except Exception:  # noqa: BLE001 - 备用系统写入失败不影响主流程
            pass


def render_conf(conf):
    return CONF_TMPL.format(
        enabled=conf.get("ENABLED", "1"),
        lock=conf.get("LOCK", "follow"),
        auto_repair=conf.get("AUTO_REPAIR", "1"),
        auto_reboot=conf.get("AUTO_REBOOT", "0"),
        loader_part=conf.get("LOADER_PART", ""),
    )


def dpkg_cfg_problem():
    """检查 dpkg 自己的配置文件能不能被解析。

    返回 (是否有问题, 原始报错, 出问题的文件名)。

    只看 `configuration error` 这一类——半装包（`dpkg --audit` 有输出但能正常解析）
    是另一回事，不在此列。这个检查存在的唯一理由：1.1.13 及以前的 dpkg 钩子
    曾把整台机器的 dpkg 卡死在配置解析阶段，而当时的 `hook_ok` 只判断文件存不存在。
    """
    rc, out, err = run(["/usr/bin/dpkg", "--audit"], timeout=20)
    text = (out or "") + "\n" + (err or "")
    if "configuration error" not in text:
        return False, "", ""
    bad = ""
    for tok in text.replace(":", " ").split():
        if tok.startswith("/etc/dpkg/") or tok.startswith("/etc/apt/"):
            bad = tok
            break
    line = ""
    for ln in text.splitlines():
        if "configuration error" in ln:
            line = ln.strip()
            break
    return True, line, bad


def repair_dpkg_cfg():
    """把被写坏的 dpkg 钩子撤掉，恢复成能被 dpkg 接受的写法。

    返回 (是否已恢复, 说明)。
    """
    broken, line, bad_file = dpkg_cfg_problem()
    if not broken:
        return True, "dpkg 配置正常"

    removed = []
    # 先撤我们自己的钩子：它是最可能的元凶，而且在 1.1.13 及以前确实是坏的
    for cand in (DPKG_HOOK_DST, bad_file):
        if not cand:
            continue
        if os.path.isfile(cand) and cand.startswith("/etc/dpkg/"):
            try:
                os.unlink(cand)
                removed.append(cand)
            except OSError:
                pass

    still_broken, line2, _ = dpkg_cfg_problem()
    if still_broken:
        return False, ("dpkg 配置仍然无法解析：%s\n已撤掉：%s"
                       % (line2 or line, "、".join(removed) or "（无）"))

    # 恢复成功，装回正确形式
    try:
        os.makedirs(os.path.dirname(DPKG_WRAPPER_DST), exist_ok=True)
        write_file(DPKG_WRAPPER_DST, DPKG_WRAPPER, 0o755)
        write_file(DPKG_HOOK_DST, DPKG_HOOK, 0o644)
        ok, _, _ = dpkg_cfg_problem()
        if ok:          # dpkg_cfg_problem 返回的是"有问题"，不是"没问题"
            os.unlink(DPKG_HOOK_DST)
            return True, ("dpkg 已恢复正常；新钩子仍不被接受，已保持移除状态（"
                          "同步改由 20 秒监视负责，功能不受影响）")
    except OSError:
        pass
    return True, ("dpkg 已恢复正常（撤掉了 %s，重装为无引号形式）"
                  % ("、".join(removed) or "损坏的钩子"))


def dpkg_cfg_ok():
    return not dpkg_cfg_problem()[0]


def _install_into_root(rootdir, script_src, conf_text):
    """把脚本与单元装进指定 rootfs（rootdir="/" 或挂载点）。"""
    joined = lambda p: os.path.join(rootdir, p.lstrip("/"))
    script_dst = joined(SCRIPT_DST)
    os.makedirs(os.path.dirname(script_dst), exist_ok=True)
    shutil.copyfile(script_src, script_dst)
    os.chmod(script_dst, 0o755)

    unit_dir = joined(UNIT_DIR)
    os.makedirs(unit_dir, exist_ok=True)
    write_file(os.path.join(unit_dir, "oesboot-sync.service"), UNIT_SYNC, 0o644)
    write_file(os.path.join(unit_dir, "oesboot-shutdown-sync.service"), UNIT_SHUTDOWN, 0o644)
    write_file(os.path.join(unit_dir, "oesboot-check.service"), UNIT_CHECK, 0o644)
    write_file(os.path.join(unit_dir, "oesboot-check.timer"), UNIT_TIMER, 0o644)
    write_file(os.path.join(unit_dir, "oesboot-watch.service"), UNIT_WATCH, 0o644)
    write_file(os.path.join(unit_dir, "oesboot-watch.timer"), UNIT_WATCH_TIMER, 0o644)

    apt_dir = joined("/etc/apt/apt.conf.d")
    os.makedirs(apt_dir, exist_ok=True)
    write_file(os.path.join(apt_dir, "99oesboot-sync"), APT_HOOK, 0o644)

    dpkg_dir = joined("/etc/dpkg/dpkg.cfg.d")
    os.makedirs(dpkg_dir, exist_ok=True)
    wrapper_dst = joined(DPKG_WRAPPER_DST)
    os.makedirs(os.path.dirname(wrapper_dst), exist_ok=True)
    write_file(wrapper_dst, DPKG_WRAPPER, 0o755)
    hook_dst = os.path.join(dpkg_dir, "99oesboot-sync")
    write_file(hook_dst, DPKG_HOOK, 0o644)
    # 写完立刻验：dpkg 能不能读这份配置。读不了就当场撤掉钩子。
    # 宁可少一个触发点（还有 20 秒监视与关机/开机同步兜底），
    # 也绝不能把一个会让包管理器瘫痪的文件留在系统里。
    if rootdir == "/" and not dpkg_cfg_ok():
        try:
            os.unlink(hook_dst)
        except OSError:
            pass

    for unit, target in (("oesboot-sync.service", "multi-user.target"),
                         ("oesboot-shutdown-sync.service", "shutdown.target"),
                         ("oesboot-check.timer", "timers.target")):
        wants = os.path.join(unit_dir, target + ".wants")
        os.makedirs(wants, exist_ok=True)
        link = os.path.join(wants, unit)
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(os.path.join(UNIT_DIR, unit), link)

    write_file(joined(CONF_DST), conf_text, 0o644)
    return script_dst


def install(script_src, conf):
    """安装到当前 rootfs 与其他 fnOS rootfs。返回部署报告。"""
    report = []
    conf_text = render_conf(conf)

    # 先看 dpkg 有没有被旧版钩子写坏。坏的话所有装包都会在解析配置阶段退出，
    # 这时候装什么、升什么都必然失败——必须先恢复，再谈同步。
    fixed, note = repair_dpkg_cfg()
    report.append({"target": "dpkg", "ok": fixed, "note": note})

    _install_into_root("/", script_src, conf_text)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "oesboot-sync.service"])
    run(["systemctl", "enable", "oesboot-shutdown-sync.service"])
    run(["systemctl", "enable", "--now", "oesboot-check.timer"])
    # 升级监视刻意不 enable：只在用户点"启动升级守护"时临时 start，重启即消失
    run(["sh", SCRIPT_DST, "sync", "--reason", "install"], timeout=180)
    report.append({"target": "/", "ok": True, "note": "当前系统"})

    for part in other_rootfs():
        try:
            with Mount(part, readonly=False, prefix="oesother") as mnt:
                _install_into_root(mnt, script_src, conf_text)
            report.append({"target": part, "ok": True, "note": "备用系统（无需重启即生效）"})
        except Exception as exc:  # noqa: BLE001 - 备用系统失败不阻断
            report.append({"target": part, "ok": False, "note": str(exc)[:200]})
    return report


def uninstall():
    disable_watch()
    for unit in ("oesboot-sync.service", "oesboot-shutdown-sync.service",
                 "oesboot-check.timer", "oesboot-watch.timer"):
        run(["systemctl", "disable", unit])
        run(["systemctl", "stop", unit])
    for f in (os.path.join(UNIT_DIR, "oesboot-sync.service"),
              os.path.join(UNIT_DIR, "oesboot-shutdown-sync.service"),
              os.path.join(UNIT_DIR, "oesboot-check.service"),
              os.path.join(UNIT_DIR, "oesboot-check.timer"),
              os.path.join(UNIT_DIR, "oesboot-watch.service"),
              os.path.join(UNIT_DIR, "oesboot-watch.timer"),
              "/etc/apt/apt.conf.d/99oesboot-sync",
              "/etc/dpkg/dpkg.cfg.d/99oesboot-sync",
              SCRIPT_DST):
        try:
            os.unlink(f)
        except OSError:
            pass
    for target, unit in (("multi-user.target", "oesboot-sync.service"),
                         ("shutdown.target", "oesboot-shutdown-sync.service"),
                         ("timers.target", "oesboot-check.timer"),
                         ("timers.target", "oesboot-watch.timer")):
        link = os.path.join(UNIT_DIR, target + ".wants", unit)
        try:
            os.unlink(link)
        except OSError:
            pass
    run(["systemctl", "daemon-reload"])


# ---------------------------------------------------------------------------
# 升级监视（用户点"开始升级守护"后开启）
#   目标：OTA 自动重启前，内核必须已经落到 eMMC 引导分区。
#   三层触发，任一层成功即可：
#     1. apt/dpkg 钩子 —— 内核包安装完成的瞬间
#     2. 本监视（20s）  —— 不依赖 OTA 用什么方式写 /boot
#     3. shutdown 单元  —— 重启前的最后一道
# ---------------------------------------------------------------------------
def enable_watch(days=6):
    """开启升级监视。标记放在 /run，重启后自动消失，不会长期常驻。"""
    import time
    os.makedirs(os.path.dirname(UPGRADE_STATE), exist_ok=True)
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    write_file(UPGRADE_FLAG, started + "\n", 0o644)
    write_file(UPGRADE_STATE, "started=%s\nexpire_days=%s\n" % (started, days), 0o644)
    # 每次开新窗口都从零计数，避免上一轮的让路次数累加到这一轮
    try:
        os.unlink("/run/oes-boot-sync/ota.state")
    except OSError:
        pass
    # 启动监视 = 用户要它干活。总开关若还是关的，脚本每次都在开头
    # exit 0，界面显示"已激活"却一次都不同步（14:34 那轮就是这样：
    # 窗口开着、timer 在跑、累计同步 0 次）。开窗顺手把开关打开。
    try:
        conf = read_conf()
        if conf.get("ENABLED") != "1":
            conf["ENABLED"] = "1"
            write_conf(conf, both=True)
    except Exception:
        pass

    # --no-block：不要让 HTTP 请求等着第一次同步跑完
    run(["systemctl", "start", "--no-block", "oesboot-watch.timer"])
    run(["systemctl", "start", "--no-block", "oesboot-watch.service"])
    return {"active": True, "since": started, "days": days}


def disable_watch():
    try:
        os.unlink(UPGRADE_FLAG)
    except OSError:
        pass
    run(["systemctl", "stop", "oesboot-watch.timer"], timeout=15)
    return {"active": False}


def watch_status():
    from .util import run_out
    active = os.path.isfile(UPGRADE_FLAG)
    since = read_file(UPGRADE_FLAG).strip() if active else ""
    timer = run_out(["systemctl", "is-active", "oesboot-watch.timer"]) or "unknown"
    return {"active": active, "since": since, "timer": timer, "interval": 20}


_OTHER_CACHE = {"ts": 0.0, "list": []}

def other_rootfs(max_age=60):
    """找出除当前系统外的 fnOS 根分区（通常是 eMMC 的 p2）。"""
    import time
    from . import sysinfo
    now = time.time()
    if _OTHER_CACHE["list"] and now - _OTHER_CACHE["ts"] < max_age:
        return _OTHER_CACHE["list"]
    info = sysinfo.collect()
    cur = info["chain"]["root_dev"]
    out = []
    for disk in info["disks"]:
        for part in disk["partitions"]:
            if part["path"] == cur:
                continue
            if part["role"] == "fnos-root" and not part["mount"]:
                out.append(part["path"])
    _OTHER_CACHE["ts"] = now
    _OTHER_CACHE["list"] = out
    return out


def log_tail(n=40):
    lines = read_file("/var/log/oesboot-guard.log").splitlines()
    return lines[-n:]


def append_note(msg):
    append_file("/var/log/oesboot-guard.log", msg + "\n")
