"""设备与启动链探测。"""
import json
import os
import re
import time

from . import bootenv
from .util import (Mount, base_of, blkid_value, human_size, load_json, read_file,
                   run, run_out)

VERSION_FILES = ("/usr/trim/etc/version", "/etc/trim/version", "/etc/issue")
FAKE_ENV = "OESBOOT_FAKE"
_cache = {"versions": {}, "ts": 0.0}


# ---------------------------------------------------------------------------
# 基础读取
# ---------------------------------------------------------------------------
def cmdline():
    return read_file("/proc/cmdline").strip()


def ubootpart():
    m = re.search(r"(?:^|\s)ubootpart=(\S+)", cmdline())
    return m.group(1) if m else ""


def findmnt_source(target):
    return run_out(["findmnt", "-n", "-o", "SOURCE", "--target", target])


def findmnt_target(dev):
    return run_out(["findmnt", "-n", "-o", "TARGET", dev])


def real_dev(dev):
    if not dev:
        return ""
    if dev.startswith("/dev/mapper/"):
        resolved = run_out(["readlink", "-f", dev])
        return resolved or dev
    return dev


def pkname(dev):
    if not dev or not os.path.exists(dev):
        return ""
    return run_out(["lsblk", "-dn", "-o", "PKNAME", dev])


def is_emmc_disk(name):
    return bool(re.match(r"^mmcblk\d+$", name or ""))


def is_removable_kernel_part(name):
    return bool(re.search(r"(boot\d+|rpmb)$", name or ""))


def emmc_boot_part():
    for entry in sorted(os.listdir("/sys/block")):
        if not re.match(r"^mmcblk\d+$", entry):
            continue
        cand = "/dev/%sp1" % entry
        if os.path.exists(cand):
            return cand
    return ""


def read_version(rootdir):
    """从已挂载的根目录读 fnOS 版本。"""
    for rel in VERSION_FILES:
        path = os.path.join(rootdir, rel)
        if not os.path.isfile(path):
            continue
        text = read_file(path).strip()
        if not text:
            continue
        if rel.endswith("/issue"):
            m = re.search(r"fnOS[^\n]*?([0-9]+\.[0-9]+(?:\.[0-9]+)*)", text, re.I)
            if m:
                return m.group(1)
            m = re.search(r"([0-9]+\.[0-9]+(?:\.[0-9]+)*)", text)
            if m:
                return m.group(1)
        else:
            m = re.search(r"([0-9]+\.[0-9]+(?:\.[0-9]+)*)", text)
            return m.group(1) if m else text.splitlines()[0][:32]
    return ""


def app_version():
    """守护脚本的版本号（与安装包同编号），用来确认设备上装的是哪一版。"""
    from . import guard as _guard
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (_guard.SCRIPT_DST,
                 os.path.join(os.path.dirname(here), "assets", "oes-boot-sync.sh")):
        m = re.search(r'^VERSION="([^"]+)"', read_file(path), re.M)
        if m:
            return m.group(1)
    return ""


def probe_version(dev):
    """挂载分区（只读）读取系统版本，带缓存。"""
    key = dev
    now = time.time()
    cached = _cache["versions"].get(key)
    if cached and now - cached[0] < 120:
        return cached[1]
    version = ""
    try:
        with Mount(dev, readonly=True, prefix="oesprobe") as mnt:
            version = read_version(mnt)
    except RuntimeError:
        version = ""
    _cache["versions"][key] = (now, version)
    return version


def lsblk():
    cols = "NAME,KNAME,PATH,TYPE,SIZE,FSTYPE,LABEL,UUID,PARTUUID,MOUNTPOINTS,RO,MODEL,TRAN"
    rc, out, _ = run(["lsblk", "-J", "-b", "-o", cols])
    if rc != 0 or not out.strip():
        cols = "NAME,KNAME,PATH,TYPE,SIZE,FSTYPE,LABEL,UUID,PARTUUID,MOUNTPOINTS,RO"
        rc, out, _ = run(["lsblk", "-J", "-b", "-o", cols])
    data = load_json(out, {})
    return data.get("blockdevices", []) or []


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return v


def _mountpoints(dev):
    v = dev.get("mountpoints")
    return [p for p in _as_list(v) if p]


# ---------------------------------------------------------------------------
# 主采集
# ---------------------------------------------------------------------------
def _mounts():
    """挂载相关原始信息：fstab、findmnt、systemd mount 单元。

    「安装阶段失败」多半和 /boot 挂在哪有关。OES 的 u-boot 永远从 eMMC
    引导分区读内核，如果 /boot 还挂在 SSD 上，OTA 写的新内核就进不了
    u-boot 真正读的分区，升级器会判定内核没换上而失败。这段信息用于
    确认 /boot 与 /vol00/RemovableDisk 各自挂在哪、由谁负责挂。
    """
    out = {
        "fstab": read_file("/etc/fstab"),
        "findmnt": run_out(["findmnt", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS"]),
        "mount_units": run_out(["systemctl", "list-units", "--type=mount",
                                "--all", "--no-pager", "--no-legend"]),
        "fstab_d": run_out(["sh", "-c",
                            "for f in /etc/fstab.d/*; do [ -f \"$f\" ] && echo \"# $f\" && cat \"$f\"; done 2>/dev/null"]),
    }
    return out


def collect(force=False):
    fake = os.environ.get(FAKE_ENV)
    if fake and os.path.isfile(fake):
        data = load_json(read_file(fake), {})
        if data:
            if not data.get("app_version"):
                data["app_version"] = app_version()
            return data

    root_dev = real_dev(findmnt_source("/"))
    root_pu = blkid_value(root_dev, "PARTUUID")
    root_base = base_of(root_pu)
    boot_dir = "/boot"
    boot_target = run_out(["findmnt", "-n", "-o", "TARGET", "--target", boot_dir])
    if boot_target == boot_dir:
        boot_part = real_dev(findmnt_source(boot_dir))
    else:
        # /boot 不是独立分区（直接落在根文件系统上）
        boot_part = root_dev
    ub_part = ubootpart()
    lp = ub_part if os.path.exists(ub_part or "") else emmc_boot_part()

    disks = []
    for d in lsblk():
        if d.get("type") != "disk":
            continue
        name = d.get("name") or d.get("kname") or ""
        disk = {
            "name": name,
            "path": d.get("path") or "/dev/" + name,
            "model": (d.get("model") or "").strip(),
            "size": int(d.get("size") or 0),
            "size_text": human_size(d.get("size") or 0),
            "tran": (d.get("tran") or "").strip(),
            "ro": bool(d.get("ro")),
            "emmc": is_emmc_disk(name),
            "is_system": name == pkname(root_dev),
            "partitions": [],
        }
        for p in (d.get("children") or []):
            pname = p.get("name") or ""
            if is_removable_kernel_part(pname):
                continue
            path = p.get("path") or "/dev/" + pname
            pu = p.get("partuuid") or ""
            fstype = (p.get("fstype") or "").strip()
            mp = _mountpoints(p)
            part = {
                "name": pname,
                "path": path,
                "size": int(p.get("size") or 0),
                "size_text": human_size(p.get("size") or 0),
                "fstype": fstype,
                "label": (p.get("label") or "").strip(),
                "partuuid": pu,
                "base": base_of(pu),
                "mount": mp[0] if mp else "",
                "role": "unknown",
                "version": "",
                "avail_mb": -1, "total_mb": -1,
            }
            if part["mount"]:
                try:
                    part["avail_mb"], part["total_mb"] = _mf_mb(part["mount"])
                except (OSError, ValueError):
                    pass
            if mp and "/" in mp:
                part["role"] = "root"
            elif path == boot_part:
                part["role"] = "boot"
            elif path == lp:
                part["role"] = "loader"
            elif fstype == "vfat" and not part["mount"]:
                part["role"] = "boot?"
            disk["partitions"].append(part)
        disks.append(disk)

    # 版本探测：只探测"像系统盘"的磁盘（同盘上有 FAT 启动分区），
    # 避免把数据卷挂起来（数据卷上不会有 /usr/trim/etc/version）
    for disk in disks:
        has_boot = any(p["fstype"] == "vfat" for p in disk["partitions"])
        if not has_boot:
            continue
        for part in disk["partitions"]:
            if part["role"] in ("unknown", "boot?") and part["fstype"] == "btrfs" and part["size"] > 2 << 30:
                part["version"] = probe_version(part["path"])
                if part["version"]:
                    part["role"] = "fnos-root"

    # 启动链状态
    boot_env_text = read_file(os.path.join(boot_dir, "fnEnv.txt"))
    loader_env_text = ""
    loader_kernel = ""
    loader_env_root = ""
    loader_files = []
    loader_avail_kb = 0
    loader_last_sync = {}
    loader_last_shutdown = {}
    loader_has_kernel_map = {}
    if lp:
        try:
            with Mount(lp, readonly=True, prefix="oesloader") as mnt:
                loader_env_text = read_file(os.path.join(mnt, "fnEnv.txt"))
                names = bootenv.filenames(loader_env_text)
                loader_kernel = names["kernelfile"]
                loader_env_root = names["rootdev"]
                loader_has_kernel_map = names
                loader_files = sorted(
                    f for f in os.listdir(mnt) if f.startswith(("vmlinuz-", "uInitrd-", "initrd.img-"))
                )
                rc, out, _ = run(["df", "-kP", mnt])
                if rc == 0 and len(out.splitlines()) > 1:
                    loader_avail_kb = int(out.splitlines()[1].split()[3])
                # 同步记录写在引导分区（1.2.07xx 的 /boot 下没有）
                for line in read_file(os.path.join(mnt, ".oesboot", "last-sync")).splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        loader_last_sync[k.strip()] = v.strip()
                for line in read_file(os.path.join(mnt, ".oesboot", "last-shutdown")).splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        loader_last_shutdown[k.strip()] = v.strip()
        except (RuntimeError, OSError, ValueError):
            pass

    boot_names = bootenv.filenames(boot_env_text)
    running_kernel = run_out(["uname", "-r"])

    guard = guard_status()
    if not guard.get("last_sync") and loader_last_sync:
        guard["last_sync"] = loader_last_sync
    # 配置里的 LOADER_PART 通常留空（自动探测），报告里显示实际值更有用
    guard["loader_part_configured"] = guard.get("loader_part") or "自动探测"
    guard["loader_part"] = lp

    info = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "app_version": app_version(),
        "host": {
            "hostname": run_out(["hostname"]),
            "kernel": running_kernel,
            "arch": run_out(["uname", "-m"]),
            "version": read_version("/"),
            "uptime": read_file("/proc/uptime").split(" ")[0] if read_file("/proc/uptime") else "",
        },
        "chain": {
            "root_dev": root_dev,
            "root_partuuid": root_pu,
            "root_base": root_base,
            "root_disk": pkname(root_dev),
            "root_on_emmc": is_emmc_disk(pkname(root_dev)),
            "boot_dir": boot_dir,
            "boot_part": boot_part,
            "boot_base": base_of(blkid_value(boot_part, "PARTUUID")) if boot_part else "",
            "ubootpart": ub_part,
            "loader_part": lp,
            "loader_base": base_of(blkid_value(lp, "PARTUUID")) if lp else "",
            "loader_avail_kb": loader_avail_kb,
            "cmdline": cmdline(),
            "boot_env": boot_env_text,
            "boot_env_files": boot_names,
            "loader_env": loader_env_text,
            "loader_env_kernel": loader_kernel,
            "loader_env_root": loader_env_root,
            "loader_files": loader_files,
        },
        "disks": disks,
        "guard": guard,
        "mounts": _mounts(),
        "space": {
            "boot_avail_mb": _mf_mb(boot_dir)[0],
            "boot_total_mb": _mf_mb(boot_dir)[1],
            "root_avail_mb": _mf_mb("/")[0],
            "root_total_mb": _mf_mb("/")[1],
        },
    }
    info["support"] = support_of(info)
    info["issues"] = analyze(info)
    info["upgrade"] = upgrade_status(info, loader_has_kernel_map, loader_last_shutdown)
    return info


# ---------------------------------------------------------------------------
# 升级就绪判定
#   回答一个问题：现在去点「系统升级」，会不会失联？
#   判据只有三条：
#     1) 引导分区此刻能否正确引导当前系统（内核 / rootdev / 设备树）
#     2) 新内核写进 /boot 之后，有没有东西能在自动重启前把它搬过去
#     3) 引导分区装不装得下
# ---------------------------------------------------------------------------
def _mf_mb(path):
    """返回 (可用 MB, 总 MB)；失败返回 (-1, -1)。"""
    try:
        st = os.statvfs(path)
    except OSError:
        return -1, -1
    fr = st.f_frsize or st.f_bsize or 4096
    return int(st.f_bavail * fr / (1 << 20)), int(st.f_blocks * fr / (1 << 20))


OTA_HOLD = "/run/oes-boot-sync/ota-hold"


def _ota_hold():
    """升级期间守护的让路状态。

    OTA 安装阶段守护会暂停周期同步（避免和 OTA 抢引导分区），
    状态写在 /run/oes-boot-sync/ota-hold，OTA 退出后由下一轮同步清除。
    """
    out = {"busy": False, "stale": False, "since": "", "last": "",
           "count": 0, "pid": "", "cmd": ""}
    if not os.path.isfile(OTA_HOLD):
        return out
    out["busy"] = True
    for line in read_file(OTA_HOLD).splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k == "count":
            out["count"] = int(v) if v.isdigit() else 0
        elif k in out:
            out[k] = v
    # 超过 5 分钟没更新，说明定时器没在跑（或已结束后没清干净），不能当真
    last = out.get("last") or ""
    if len(last) >= 19:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if now > last:
            try:
                t1 = time.mktime(time.strptime(last, "%Y-%m-%d %H:%M:%S"))
                t2 = time.mktime(time.strptime(now, "%Y-%m-%d %H:%M:%S"))
                if t2 - t1 > 300:
                    out["stale"] = True
                    out["busy"] = False
            except (ValueError, OverflowError):
                pass
    return out


OTA_STATE = "/run/oes-boot-sync/ota.state"
UPGRADE_FLAG = "/run/oesboot-upgrade"
OTA_BASELINE = "/var/lib/oesboot/ota-baseline.txt"
OTA_LAST = "/var/lib/oesboot/ota-last.txt"
OTA_REPORT = "/var/lib/oesboot/ota-report.txt"
OTA_EXPORT = "/var/lib/oesboot/last-export.txt"   # 上次另存到存储空间的路径


def _read_kv(path):
    """读 key=value 形式的小文件（守护写的都是这种）。"""
    out = {}
    for line in read_file(path).splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _ota_result():
    """上一轮升级的结算结果。

    守护在升级窗口关闭（含系统自动重启）后的第一次定时任务里写这个文件，
    放在 /var/lib 而不是 /run，所以重启后仍然读得到——这正是之前
    「失败后无从判断」的缺口。
    """
    out = {"state": "", "note": "", "since": "", "finished": "",
           "version_before": "", "version_after": "",
           "kernel_before": "", "kernel_after": "", "span": "", "yields": "",
           "fail_pkg": "", "fail_line": "",
           "has_report": False, "report_size": 0}
    if not os.path.isfile(OTA_LAST):
        return out
    out.update({k: v for k, v in _read_kv(OTA_LAST).items() if k in out})
    if os.path.isfile(OTA_REPORT):
        out["has_report"] = True
        try:
            out["report_size"] = os.path.getsize(OTA_REPORT)
        except OSError:
            out["report_size"] = 0
    return out


def _ota_report_meta():
    """升级现场报告文件本身的信息。

    和 _ota_result 分开：报告可以手动生成（窗口进行中也能抓），
    所以「有没有报告」不该取决于有没有结算记录。
    """
    out = {"exists": False, "size": 0, "time": "", "age": -1,
           "saved_path": "", "saved_time": ""}
    try:
        st = os.stat(OTA_REPORT)
    except OSError:
        pass
    else:
        out["exists"] = True
        out["size"] = st.st_size
        try:
            out["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
            out["age"] = int(time.time() - st.st_mtime)
        except (OSError, ValueError):
            pass
    if os.path.isfile(OTA_EXPORT):
        kv = _read_kv(OTA_EXPORT)
        out["saved_path"] = kv.get("path", "")
        out["saved_time"] = kv.get("time", "")
    return out


def _ota_window():
    """升级窗口：从点了「开始升级守护」到系统重启。

    窗口内守护完全不写引导分区（OTA 要独占写），所以"同步次数"会停住。
    这里把窗口状态和让路次数单独取出来，UI 才能说明白"它在让路"而不是"它挂了"。
    """
    out = {"active": False, "since": "", "age": -1, "yields": 0, "last": "",
           "reason": "", "boot_kernel": "", "loader_kernel": "", "same": "",
           "base_version": "", "base_kernel": "", "cur_version": "",
           "cur_kernel": ""}
    if os.path.isfile(UPGRADE_FLAG):
        try:
            out["active"] = True
            out["since"] = read_file(UPGRADE_FLAG).strip()
            out["age"] = int(time.time() - os.stat(UPGRADE_FLAG).st_mtime)
        except OSError:
            pass
    if os.path.isfile(OTA_STATE):
        for line in read_file(OTA_STATE).splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "yields":
                out["yields"] = int(v) if v.isdigit() else 0
            elif k in out:
                out[k] = v
    return out


OTA_TRACE = "/var/lib/oesboot/ota-trace.log"
OTA_TRACE_FILE = "/var/lib/oesboot/ota-trace-file.log"


def _ota_trace(limit=80):
    """升级期间守护抄下来的 journald 日志。

    升级失败后 fnOS 可能清掉自己的日志，这里是唯一保留下来的现场。
    """
    if not os.path.isfile(OTA_TRACE):
        return []
    return read_file(OTA_TRACE).splitlines()[-limit:]


def _ota_trace_file(limit=120):
    """升级器写在文件里的日志（/usr/trim、/var/log 下的增量抄录）。

    fnOS 的升级器基本不往 journald 打自己的日志，真正的报错在这一份里。
    """
    if not os.path.isfile(OTA_TRACE_FILE):
        return []
    return read_file(OTA_TRACE_FILE).splitlines()[-limit:]


# 系统更新器可能写日志的位置（不同版本不一样，宁可多找几个）
_OTA_LOG_GLOBS = (
    "/var/log/fnos*.log", "/var/log/fnos/*.log", "/var/log/fnos/**/*.log",
    "/var/log/trim*.log", "/var/log/trim/*.log",
    "/var/log/*update*.log", "/var/log/*upgrade*.log", "/var/log/ota*.log",
    "/usr/trim/log/*.log", "/usr/trim/var/log/*.log",
    "/var/log/sysupdate*.log", "/var/log/system_update*.log",
)


def _ota_logs(limit=2, tail=20):
    """找出最近的系统升级日志，取末尾若干行。用于定位"安装失败"。"""
    import glob as _glob
    seen = set()
    picked = []
    now = time.time()
    for pat in _OTA_LOG_GLOBS:
        try:
            hits = _glob.glob(pat, recursive=True)
        except TypeError:
            hits = _glob.glob(pat)
        for p in hits:
            if p in seen:
                continue
            seen.add(p)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if not os.path.isfile(p) or st.st_size == 0:
                continue
            if now - st.st_mtime > 3 * 86400:
                continue
            picked.append((st.st_mtime, p))
    picked.sort(reverse=True)
    out = []
    for mtime, p in picked[:limit]:
        out.append({
            "path": p,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
            "lines": read_file(p).splitlines()[-tail:],
        })
    return out


def _watch_hits(since):
    """升级监视窗口内实际执行的次数，给用户一个"它在干活"的证据。

    1.1.1 起监视没变化时不写日志，所以这里优先用脚本自己维护的计数器；
    老版本没有计数文件时退回日志行数。
    """
    raw = read_file("/run/oes-boot-sync/watch.hits").strip()
    if raw.isdigit():
        return int(raw)
    n = 0
    for line in read_file("/var/log/oesboot-guard.log").splitlines():
        if "触发=watch" not in line:
            continue
        if not since or line[:19] >= since:
            n += 1
    return n


def upgrade_status(info, loader_names=None, last_shutdown=None):
    from . import guard as guard_mod

    chain = info["chain"]
    guard = info["guard"]
    loader_names = loader_names or {}
    checks = []

    def add(key, label, ok, detail, level="error", fix=""):
        checks.append({
            "key": key, "label": label, "ok": bool(ok),
            "level": "ok" if ok else level, "detail": detail, "fix": fix,
        })

    boot = chain.get("boot_env_files") or {}
    bk = boot.get("kernelfile", "")
    lk = chain.get("loader_env_kernel", "")
    bf = boot.get("fdtfile", "")
    lf = loader_names.get("fdtfile", "")
    lroot = chain.get("loader_env_root", "")
    rbase = chain.get("root_base", "")

    add("kernel", "引导分区内核与系统一致",
        bool(bk and lk and bk == lk),
        ("系统与引导分区均为 %s" % bk) if (bk and bk == lk)
        else ("系统 /boot：%s　引导分区：%s" % (bk or "缺失", lk or "缺失")),
        fix="sync")
    root_ok = bool(rbase and lroot and rbase in lroot)
    add("rootdev", "引导指向当前系统根分区", root_ok,
        ("rootdev=%s，与当前根分区一致" % lroot) if root_ok
        else ("rootdev=%s　当前根分区=%s" % (lroot or "缺失", chain.get("root_partuuid") or "缺失")),
        fix="sync")
    # 最容易被忽略、但最致命的一项：u-boot 从哪个分区读内核，和 /boot 挂在哪。
    # OES 的 u-boot 写死 ubootpart=/dev/mmcblk1p1，只认 eMMC 引导分区。
    # /boot 若还挂在 SSD，OTA 把新内核写进 SSD 后 u-boot 读的还是 eMMC 里的旧内核，
    # 升级器核对"内核没换上"就在安装阶段判定失败——这和守护让不让路毫无关系。
    bp_dev = chain.get("boot_part") or ""
    lp_dev = chain.get("loader_part") or ""
    boot_dev = real_dev(bp_dev) if bp_dev else ""
    loader_dev = real_dev(lp_dev) if lp_dev else ""
    if not lp_dev:
        # 探测不到 u-boot 读取的分区就不下结论，免得把"没探到"误报成"布局错"
        add("layout", "引导布局符合 OTA 要求", False,
            "没能确认 u-boot 从哪个分区读内核（/proc/cmdline 里没有 ubootpart，也找不到 eMMC 引导分区）",
            level="warn", fix="")
    elif boot_dev and boot_dev == loader_dev:
        add("layout", "引导布局符合 OTA 要求", True,
            "/boot 就挂在 u-boot 读取的引导分区 %s，OTA 写的新内核 u-boot 能直接读到" % lp_dev)
    else:
        add("layout", "引导布局符合 OTA 要求", False,
            "u-boot 只从 %s 读内核，而 /boot 挂在 %s。新内核写进 %s 之后，"
            "重启时 u-boot 读的还是 %s 里的旧内核，得靠守护把它搬过去。"
            "这一项不影响装包，也不影响现在启动；想彻底理顺再点「先修布局」"
            % (lp_dev or "eMMC 引导分区", bp_dev or "未知", bp_dev or "/boot", lp_dev or "eMMC"),
            level="warn", fix="hybrid")

    add("fdt", "设备树一致", bf == lf,
        (bf or "两边都未设置") if bf == lf
        else ("系统：%s　引导分区：%s" % (bf or "未设置", lf or "未设置")),
        level="warn", fix="sync")

    avail = chain.get("loader_avail_kb") or 0
    add("space", "引导分区空间足够", avail >= 100 * 1024,
        "剩余 %.0f MB（新内核 + 旧内核保留约需 100 MB）" % (avail / 1024.0),
        level="warn", fix="sync")

    # /boot 是 OTA 解包内核与 initrd 的地方，装不下就会在安装阶段直接失败
    b_avail, b_total = _mf_mb(chain.get("boot_dir") or "/boot")
    if b_avail >= 0 and b_total > 0 and b_total < 4096:
        add("bootspace", "/boot 空间足够", b_avail >= 100,
            "剩余 %d MB / 共 %d MB（OTA 解包内核与 initrd 一般需要 60~120 MB）"
            % (b_avail, b_total), level="warn", fix="")

    add("guard", "升级守护已部署",
        bool(guard.get("installed") and guard.get("enabled")),
        "守护%s，%s" % ("已安装" if guard.get("installed") else "未安装",
                     "已启用" if guard.get("enabled") else "未启用"),
        fix="install")

    sh_link = "/etc/systemd/system/shutdown.target.wants/oesboot-shutdown-sync.service"
    sh_ok = os.path.islink(sh_link) or os.path.isfile(sh_link)
    hook_ok = any(os.path.isfile(p) for p in
                  ("/etc/apt/apt.conf.d/99oesboot-sync", "/etc/dpkg/dpkg.cfg.d/99oesboot-sync"))

    # 这一项是 1.1.14 加的，代价是五轮排查：
    # 之前只判断钩子文件「在不在」，文件在就算通过，于是界面一路绿灯；
    # 而真实情况是那个文件写坏了，dpkg 每次操作都在解析配置阶段退出，
    # 系统升级 4 秒报「failed to install」，内核压根没解包。
    # 现在改为真的让 dpkg 读一遍配置——能不能装包，以它自己说了算。
    try:
        cfg_broken, cfg_line, cfg_file = guard_mod.dpkg_cfg_problem()
    except Exception:  # noqa: BLE001 - 探测失败不阻塞整页
        cfg_broken, cfg_line, cfg_file = False, "", ""
    add("dpkgcfg", "dpkg 配置可正常解析", not cfg_broken,
        ("dpkg 无法解析 %s，任何装包/升级都会在开始前直接失败：%s"
         % (cfg_file or "配置文件", cfg_line or "configuration error"))
        if cfg_broken else "dpkg 能正常读取 /etc/dpkg/dpkg.cfg.d（已实际执行 dpkg --audit 验证）",
        fix="dpkgfix")

    add("preboot", "重启前会自动同步", sh_ok,
        "关机同步单元%s；apt/dpkg 钩子%s" % ("已就位" if sh_ok else "缺失",
                                       "已安装" if hook_ok else "缺失"),
        level="warn", fix="install")

    failed = [c for c in checks if not c["ok"]]
    hard = [c for c in failed if c["level"] == "error"]
    dpkg_bad = any(c["key"] == "dpkgcfg" and not c["ok"] for c in checks)
    layout_bad = any(c["key"] == "layout" and not c["ok"] for c in checks)
    # dpkg 坏掉排在最前：它一坏，装什么都是秒失败，连"修布局"的脚本都可能装不上
    if dpkg_bad:
        verdict = "block"
        headline = "dpkg 配置坏了，现在什么都装不上"
        summary = ("/etc/dpkg/dpkg.cfg.d 下的配置文件无法解析，dpkg 在每次操作前就直接退出，"
                   "所以系统升级会在几秒内报「安装失败」，新内核连解包都轮不到。"
                   "点下方「修复 dpkg 配置」自动撤掉损坏的钩子并重装为正确写法，"
                   "修完不需要重启，直接去升一次。")
    elif layout_bad:
        verdict = "warn"
        headline = "引导布局不规范，建议修正"
        summary = ("u-boot 只从 eMMC 引导分区读内核，而 /boot 还挂在 SSD 上。"
                   "新内核装进 /boot 后 u-boot 读不到，重启后系统仍是旧版本。"
                   "这一项不影响装包，现在就可以直接去升级——关机同步会把新内核补到 eMMC；"
                   "想彻底理顺，再点「先修布局」，重启后生效。")
    elif hard:
        verdict = "block"
        headline = "先修复再升级"
        summary = ("引导分区和当前系统不一致，现在升级会以旧内核引导新系统，"
                   "大概率卡在启动阶段。点下方按钮一键修复，修好后再升级。")
    elif failed:
        verdict = "warn"
        headline = "可以升级，有 %d 项建议处理" % len(failed)
        summary = "升级可以正常进行；下面标黄的项不影响自动同步，方便时再处理即可。"
    else:
        verdict = "safe"
        headline = "可以升级"
        summary = ("现在去「系统设置 → 更新和还原」点升级即可。升级过程中不用做任何事："
                   "新内核写进 /boot 后会被自动搬到 eMMC 引导分区，系统自动重启也在同步之后。")

    watch = guard_mod.watch_status()
    watch["hits"] = _watch_hits(watch.get("since") or "")
    watch["recent"] = [ln for ln in read_file("/var/log/oesboot-guard.log").splitlines()
                       if "触发=watch" in ln or "触发=shutdown" in ln][-5:]
    win = _ota_window()
    watch["window"] = win
    # 让路是"此刻正在发生"的状态，不是历史累计。
    # 以前用 yields>0 判定：只要窗口里让过一次，之后哪怕早已恢复同步、
    # 界面也一直挂着"让路中"，用户会以为升级卡住了。
    # 现在只看脚本刚写的瞬时标记（/run/oes-boot-sync/holding），90 秒内有效。
    try:
        _hold_age = time.time() - os.path.getmtime("/run/oes-boot-sync/holding")
        _yielding = _hold_age < 90
    except OSError:
        _yielding = False
    watch["yielding"] = bool(win.get("active") and _yielding)

    ls = last_shutdown or {}
    pre = {
        "synced": bool(ls),
        "time": ls.get("time", ""),
        "kernelfile": ls.get("kernelfile", ""),
        "matches": bool(ls and ls.get("kernelfile") == bk),
        "reason": ls.get("reason", ""),
    }

    hooks = {
        "apt": os.path.isfile("/etc/apt/apt.conf.d/99oesboot-sync"),
        "dpkg": os.path.isfile("/etc/dpkg/dpkg.cfg.d/99oesboot-sync"),
        "shutdown": sh_ok,
        "watch": os.path.isfile("/etc/systemd/system/oesboot-watch.timer"),
        "boot": (os.path.islink("/etc/systemd/system/multi-user.target.wants/oesboot-sync.service")
                 or os.path.isfile("/etc/systemd/system/multi-user.target.wants/oesboot-sync.service")),
    }

    return {
        "verdict": verdict,
        "ready": verdict != "block",
        "headline": headline,
        "summary": summary,
        "checks": checks,
        "failed": len(failed),
        "hooks": hooks,
        "watch": watch,
        "ota": _ota_hold(),
        "window": win,
        "result": _ota_result(),
        "report": _ota_report_meta(),
        "last_shutdown": pre,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def support_of(info):
    chain = info["chain"]
    reasons = []
    if not chain["loader_part"]:
        reasons.append("未找到 eMMC 启动分区（/dev/mmcblk*p1）")
    if not chain["boot_env"]:
        reasons.append("未找到 /boot/fnEnv.txt")
    if not chain["root_base"]:
        reasons.append("根分区没有 PARTUUID（非 OES 引导结构）")
    return {"supported": not reasons, "reasons": reasons}


def analyze(info):
    """生成问题清单，供 UI 展示。"""
    chain = info["chain"]
    guard = info["guard"]
    issues = []

    def add(level, code, title, detail=""):
        issues.append({"level": level, "code": code, "title": title, "detail": detail})

    if not info.get("support", {}).get("supported"):
        add("error", "unsupported", "当前设备不具备 OES 引导结构",
            "；".join(info["support"]["reasons"]))
        return issues

    boot_kernel = chain["boot_env_files"]["kernelfile"]
    loader_kernel = chain["loader_env_kernel"]
    env_root = chain["loader_env_root"]

    if boot_kernel and loader_kernel and boot_kernel != loader_kernel:
        add("error", "kernel-desync",
            "启动分区内核与系统不一致（升级后失联的直接原因）",
            "系统 /boot 使用 %s，eMMC 引导分区仍是 %s。下次重启会以旧内核加载新系统。" % (boot_kernel, loader_kernel))
    elif boot_kernel and not loader_kernel:
        add("error", "loader-env-missing", "引导分区缺少 kernelfile 配置",
            "eMMC 上的 fnEnv.txt 没有 kernelfile，u-boot 可能无法加载内核。")

    if env_root and chain["root_base"] and chain["root_base"] not in env_root:
        add("warn", "rootdev-mismatch", "引导指向的系统与当前运行系统不同",
            "fnEnv.txt rootdev=%s，当前运行根分区是 %s。" % (env_root, chain["root_partuuid"]))

    if chain["root_on_emmc"] and guard.get("lock") == "ssd":
        add("warn", "emmc-fallback", "当前从 eMMC 启动，策略要求 SSD",
            "守护会在下次开机自动把引导切回 SSD（如已启用自动修复）。")

    if not guard.get("installed"):
        add("warn", "no-guard", "升级守护未安装", "安装应用时会自动部署，可点\"修复守护\"重新安装。")
    elif not guard.get("enabled"):
        add("warn", "guard-disabled", "升级守护已停用",
            "停用后系统升级会再次出现\"升级后无法启动\"的问题。")

    if chain["loader_avail_kb"] and chain["loader_avail_kb"] < 80 * 1024:
        add("warn", "loader-space", "eMMC 引导分区剩余空间偏小",
            "剩余 %.0f MB，新内核+initrd 通常需要 80~100MB。" % (chain["loader_avail_kb"] / 1024.0))

    if not issues:
        add("ok", "healthy", "启动链一致", "引导分区内核与当前系统一致，可放心升级。")
    return issues


# ---------------------------------------------------------------------------
# 守护状态
# ---------------------------------------------------------------------------
def guard_status():
    conf = read_file("/etc/oesboot.conf")
    data = {}
    for line in conf.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip().strip('"')
    installed = os.path.isfile("/usr/local/sbin/oes-boot-sync")
    enabled = False
    if os.path.isdir("/etc/systemd/system"):
        for want in ("multi-user.target.wants", "shutdown.target.wants"):
            if os.path.islink("/etc/systemd/system/%s/oesboot-sync.service" % want):
                enabled = True
    last_sync = {}
    syncfile = "/boot/.oesboot/last-sync"
    if os.path.isfile(syncfile):
        for line in read_file(syncfile).splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                last_sync[k.strip()] = v.strip()
    log_path = "/var/log/oesboot-guard.log"
    log_tail = []
    if os.path.isfile(log_path):
        log_tail = read_file(log_path).splitlines()[-60:]
    return {
        "installed": installed,
        "enabled": enabled,
        # 单元挂上了不代表真的在干活：脚本开头会读 /etc/oesboot.conf 的 ENABLED，
        # 若是 0 就每次直接退出。两个条件都满足才算"守护在运行"。
        "conf_enabled": data.get("ENABLED", "1") != "0",
        "lock": data.get("LOCK", "follow"),
        "auto_repair": data.get("AUTO_REPAIR", "1") == "1",
        "auto_reboot": data.get("AUTO_REBOOT", "0") == "1",
        "loader_part": data.get("LOADER_PART", ""),
        "last_sync": last_sync,
        "log_tail": log_tail,
        "conf_raw": conf,
    }


def _fmt_value(v):
    if isinstance(v, dict):
        return ", ".join("%s=%s" % (k, v[k]) for k in v) if v else "{}"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v) if v else "[]"
    return v


def diagnose_text(info):
    """导出诊断报告（供用户复制/下载）。"""
    lines = []
    lines.append("# OES 启动链诊断报告")
    lines.append("时间: %s" % info["generated_at"])
    lines.append("主机: %s  内核: %s  架构: %s  系统版本: %s  管家: v%s"
                 % (info["host"]["hostname"], info["host"]["kernel"],
                    info["host"]["arch"], info["host"]["version"],
                    info.get("app_version") or "?"))
    lines.append("")

    up = info.get("upgrade") or {}
    _chain = info.get("chain") or {}
    if up:
        lines.append("## 升级就绪")
        lines.append("- 结论: %s（%s）" % (up.get("verdict"), up.get("headline")))
        for c in up.get("checks", []):
            lines.append("- [%s] %s — %s" % ("ok" if c["ok"] else c["level"].upper(),
                                            c["label"], c["detail"]))
        _lay = next((c for c in up.get("checks", []) if c["key"] == "layout"), None)
        if _lay:
            lines.append("- 引导布局: %s｜u-boot 读=%s｜/boot 挂=%s" % (
                "符合 OTA 要求" if _lay["ok"] else "不符合（升级装得上，但要靠守护把新内核搬过去）",
                _chain.get("loader_part") or "-", _chain.get("boot_part") or "-"))
        w = up.get("watch") or {}
        win = w.get("window") or {}
        if w.get("yielding"):
            lines.append("- 升级监视: 运行中（自 %s），升级窗口内已主动让路 %s 次（对引导分区零写入）"
                         % (win.get("since") or w.get("since", ""), win.get("yields", 0)))
        else:
            lines.append("- 升级监视: %s%s，累计已自动同步 %s 次" % (
                "运行中" if w.get("active") else "未开启",
                ("（自 %s）" % w["since"]) if w.get("active") else "",
                w.get("hits", 0)))
        if win.get("active"):
            _age = win.get("age", -1)
            _age_s = ("%d 分钟" % (_age // 60)) if isinstance(_age, int) and _age >= 0 else "未知"
            _same = {"1": "是", "0": "否"}.get(str(win.get("same", "")), "未读取")
            lines.append("- 升级窗口: 开启中（已 %s）｜升级后内核=%s｜引导分区内核=%s｜两者一致=%s"
                         % (_age_s, win.get("boot_kernel") or "-",
                            win.get("loader_kernel") or "-", _same))
        res = up.get("result") or {}
        if res.get("state"):
            _label = {"success": "成功",
                      "failed": "失败（系统版本没变）",
                      "idle": "未执行（窗口被提前关闭）"}.get(res["state"], res["state"])
            lines.append("- 上次升级: %s｜%s" % (_label, res.get("note") or ""))
            if res.get("version_before") or res.get("version_after"):
                lines.append("  版本 %s → %s｜内核 %s → %s｜窗口 %s 秒｜让路 %s 次" % (
                    res.get("version_before") or "-", res.get("version_after") or "-",
                    res.get("kernel_before") or "-", res.get("kernel_after") or "-",
                    res.get("span") or "-", res.get("yields") or "0"))
            if res.get("has_report"):
                lines.append("  现场报告: %s（%d KB，可在守护页一键导出）"
                             % (OTA_REPORT, max(1, res.get("report_size", 0) // 1024)))
        pre = up.get("last_shutdown") or {}
        lines.append("- 上次重启前同步: %s" % (
            ("%s，kernel=%s" % (pre.get("time", ""), pre.get("kernelfile", ""))) if pre.get("synced") else "无记录"))
        ota = up.get("ota") or {}
        if ota.get("busy"):
            lines.append("- 系统升级: 正在安装（%s），守护已让路 %s 次，升级结束后自动补同步"
                         % (ota.get("cmd") or "未知进程", ota.get("count", 0)))
        elif ota.get("since"):
            lines.append("- 系统升级: 上次安装期间守护让路 %s 次（%s 起）"
                         % (ota.get("count", 0), ota.get("since")))
        lines.append("")

    sp = info.get("space") or {}
    if sp:
        lines.append("## 磁盘空间")
        if sp.get("boot_total_mb", -1) > 0:
            lines.append("- /boot: 剩余 %s MB / 共 %s MB"
                         % (sp.get("boot_avail_mb"), sp.get("boot_total_mb")))
        if sp.get("root_total_mb", -1) > 0:
            lines.append("- /: 剩余 %.1f GB / 共 %.1f GB"
                         % (sp.get("root_avail_mb", 0) / 1024.0, sp.get("root_total_mb", 0) / 1024.0))
        lines.append("")

    lines.append("## 引导链")
    for k, v in info["chain"].items():
        if k in ("boot_env", "loader_env", "cmdline"):
            continue
        lines.append("- %s: %s" % (k, _fmt_value(v)))
    lines.append("")
    lines.append("## /proc/cmdline")
    lines.append("```")
    lines.append(info["chain"]["cmdline"])
    lines.append("```")
    lines.append("")
    lines.append("## /boot/fnEnv.txt")
    lines.append("```")
    lines.append(info["chain"]["boot_env"] or "(缺失)")
    lines.append("```")
    lines.append("")
    lines.append("## 引导分区 fnEnv.txt")
    lines.append("```")
    lines.append(info["chain"]["loader_env"] or "(缺失)")
    lines.append("```")
    lines.append("")
    m = info.get("mounts") or {}
    if m.get("fstab"):
        lines.append("## /etc/fstab")
        lines.append("```")
        lines.append(m["fstab"])
        lines.append("```")
        lines.append("")
        lines.append("## findmnt（挂载关系）")
        lines.append("```")
        lines.append(m.get("findmnt") or "(无)")
        lines.append("```")
        lines.append("")
        if m.get("mount_units"):
            lines.append("## systemd mount 单元")
            lines.append("```")
            lines.append(m["mount_units"])
            lines.append("```")
            lines.append("")
    lines.append("## 磁盘")
    for d in info["disks"]:
        lines.append("### %s (%s, %s)%s" % (
            d["path"], d["model"] or "-", d["size_text"], "  ← 当前系统盘" if d["is_system"] else ""))
        for p in d["partitions"]:
            lines.append("- %s  %s  %s  %s  partuuid=%s  mount=%s  role=%s%s%s" % (
                p["path"], p["size_text"], p["fstype"] or "-", p["label"] or "-",
                p["partuuid"] or "-", p["mount"] or "-", p["role"],
                ("  version=%s" % p["version"]) if p["version"] else "",
                ("  剩余 %s MB / 共 %s MB" % (p.get("avail_mb"), p.get("total_mb")))
                if p.get("total_mb", -1) > 0 else ""))
    lines.append("")
    lines.append("## 守护")
    for k, v in info["guard"].items():
        if k in ("log_tail", "conf_raw"):
            continue
        lines.append("- %s: %s" % (k, _fmt_value(v)))
    lines.append("")
    lines.append("## 最近日志")
    lines.append("```")
    lines.extend(info["guard"]["log_tail"][-40:] or ["(无)"])
    lines.append("```")

    # 升级失败时，这里往往是唯一能说明原因的地方
    trace = _ota_trace()
    if trace:
        lines.append("")
        lines.append("## 升级现场（journald）")
        lines.append("```")
        lines.extend(trace)
        lines.append("```")

    tracef = _ota_trace_file()
    if tracef:
        lines.append("")
        lines.append("## 升级现场（升级器自己的日志文件）")
        lines.append("```")
        lines.extend(tracef)
        lines.append("```")

    logs = _ota_logs()
    if logs:
        lines.append("")
        lines.append("## 系统升级日志")
        for item in logs:
            lines.append("### %s（修改于 %s）" % (item["path"], item["time"]))
            lines.append("```")
            lines.extend(item["lines"] or ["(空)"])
            lines.append("```")
    return "\n".join(lines)
