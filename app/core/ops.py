"""业务动作：同步、修复、切换启动盘、系统迁移。所有耗时操作跑在后台 Job 里。"""
import json
import os
import re
import shutil
import threading
import time
import uuid

from . import bootenv, guard, sysinfo
from .util import Mount, base_of, blkid_value, human_size, read_file, run, write_file

STATE_DIR = "/var/lib/oesboot"
BACKUP_DIR = os.path.join(STATE_DIR, "backup")
JOBS = {}
_JOB_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 任务框架
# ---------------------------------------------------------------------------
class Job(object):
    def __init__(self, kind, title):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.title = title
        self.status = "running"
        self.progress = 0
        self.steps = []
        self.result = {}
        self.started = time.time()
        self.finished = None

    def log(self, msg, level="info", progress=None):
        self.steps.append({
            "ts": time.strftime("%H:%M:%S"),
            "level": level,
            "msg": msg,
        })
        if progress is not None:
            self.progress = progress

    def ok(self, result=None):
        self.status = "success"
        self.progress = 100
        self.result = result or {}
        self.finished = time.time()

    def fail(self, msg, result=None):
        self.log(msg, "error")
        self.status = "failed"
        self.result = result or {}
        self.result["error"] = msg
        self.finished = time.time()

    def to_dict(self):
        return {
            "id": self.id, "kind": self.kind, "title": self.title,
            "status": self.status, "progress": self.progress,
            "steps": self.steps[-200:], "result": self.result,
            "elapsed": int((self.finished or time.time()) - self.started),
        }


def start_job(kind, title, fn):
    job = Job(kind, title)
    with _JOB_LOCK:
        for old_id in [k for k, v in JOBS.items() if v.finished and time.time() - v.finished > 1800]:
            JOBS.pop(old_id, None)
        JOBS[job.id] = job

    def runner():
        try:
            fn(job)
            if job.status == "running":
                job.ok()
        except Exception as exc:  # noqa: BLE001
            job.fail(str(exc))

    t = threading.Thread(target=runner, name="oesboot-" + job.id, daemon=True)
    t.start()
    return job


def get_job(job_id):
    return JOBS.get(job_id)


def running_job():
    for job in JOBS.values():
        if job.status == "running":
            return job
    return None


# ---------------------------------------------------------------------------
# 守护脚本调用
# ---------------------------------------------------------------------------
def guard_script():
    for p in ("/usr/local/sbin/oes-boot-sync", os.path.join(APP_DIR, "assets", "oes-boot-sync.sh")):
        if os.path.isfile(p):
            return p
    return ""


APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_guard(args, job=None, timeout=300):
    script = guard_script()
    if not script:
        raise RuntimeError("找不到引导同步脚本 oes-boot-sync")
    rc, out, err = run(["sh", script] + list(args), timeout=timeout)
    text = (out or "") + (("\n" + err) if err.strip() else "")
    if job:
        for line in text.splitlines():
            line = line.strip()
            if line:
                job.log(line, "warn" if "WARN" in line else ("error" if "FAIL" in line else "info"))
    return rc, text


def build_ota_report():
    """按当前状态抓一份升级现场。

    不依赖结算：升级窗口进行中、甚至还没升级过，都可以抓。
    现场只读系统信息与日志，不挂载、不写引导分区。
    """
    rc, text = run_guard(["bundle"], timeout=180)
    if rc != 0:
        raise RuntimeError((text or "抓取失败").strip()[-400:])
    if not os.path.isfile(sysinfo.OTA_REPORT):
        raise RuntimeError("现场报告未生成")
    return sysinfo.OTA_REPORT


def report_filename(ts=None):
    ts = ts or time.strftime("%Y%m%d-%H%M%S")
    return "oesboot-ota-report-%s.txt" % ts


def share_dirs():
    """用户能在文件管理器里看到的可写目录（/vol00 下的存储空间）。

    排除引导分区和 eMMC 系统分区：那两块是启动链要用的，不能往里丢文件。
    不依赖角色识别：/vol00 下能写的挂载点都算，真机上共享文件夹就是这些。
    """
    info = sysinfo.collect()
    skip = set()
    for d in info.get("disks", []):
        for pt in d.get("partitions", []):
            if pt.get("role") in ("loader", "fnos-root", "boot", "root"):
                if pt.get("mount"):
                    skip.add(pt["mount"])
    mounts, others = [], []
    for root in ("/vol00", "/vol1", "/mnt"):
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            p = os.path.join(root, name)
            if name.startswith(".") or not os.path.isdir(p):
                continue
            if p in skip:
                continue
            (mounts if os.path.ismount(p) else others).append(p)
    out = []
    for p in mounts + others:
        if p not in out:
            out.append(p)
    return out


def save_report_copy():
    """把升级现场另存一份到存储空间，返回 {ok, dir, path, name, error}。

    手机浏览器点下载经常不知道存哪儿去了，写进共享文件夹才找得到。
    只写一个小 txt，不碰引导分区。
    """
    src = sysinfo.OTA_REPORT
    if not os.path.isfile(src):
        return {"ok": False, "error": "还没有现场报告"}
    name = report_filename()
    last_err = ""
    for d in share_dirs():
        dst = os.path.join(d, name)
        tmp = os.path.join(d, ".oesboot-report.tmp")
        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
            try:
                os.makedirs(os.path.dirname(sysinfo.OTA_EXPORT), exist_ok=True)
            except OSError:
                pass
            write_file(sysinfo.OTA_EXPORT,
                       "path=%s\ntime=%s\nname=%s\n" % (dst, time.strftime("%Y-%m-%d %H:%M:%S"), name), 0o644)
            return {"ok": True, "dir": d, "path": dst, "name": name}
        except OSError as exc:
            last_err = str(exc)
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return {"ok": False, "error": last_err or "没有可写的存储空间（/vol00 下没找到）"}


def action_hybrid(job):
    """修复 OTA「安装阶段失败」的根因：把 /boot 挂到 u-boot 真正读取的 eMMC 引导分区。

    这是系统级改动（改 /etc/fstab 与引导分区 fnEnv），脚本内部已做完整备份，
    并可用「回滚」一键还原。执行后需重启生效。
    """
    job.log("预检并备份引导布局……", progress=10)
    rc, text = run_guard(["hybrid"], job, timeout=600)
    if rc != 0:
        raise RuntimeError((text or "修正失败").strip()[-600:])
    job.log("修正完成", progress=100)
    job.ok({"output": text[-4000:], "need_reboot": True})


def action_hybrid_rollback(job):
    """回滚混合引导布局，恢复修正前的 fstab 与 eMMC fnEnv。"""
    job.log("回滚引导布局……", progress=10)
    rc, text = run_guard(["hybrid-rollback"], job, timeout=600)
    if rc != 0:
        raise RuntimeError((text or "回滚失败").strip()[-600:])
    job.log("回滚完成", progress=100)
    job.ok({"output": text[-4000:], "need_reboot": True})


# ---------------------------------------------------------------------------
# 动作 1：立即同步
# ---------------------------------------------------------------------------
def action_sync(job, extra_args=None):
    job.log("开始同步引导分区……", progress=10)
    rc, text = run_guard(["sync"] + list(extra_args or []), job)
    if rc != 0:
        raise RuntimeError("同步失败，详见上方日志")

    # 脚本返回 0 不代表同步成功：让路、跳过、拒绝同步也都是 0。
    # 1.1.15 及以前这里无条件打「同步完成」，而脚本其实只输出了一行
    # "oesboot guard disabled" 就退出了——界面说成功，eMMC 上的内核没动。
    if "RESULT=done" not in text:
        if "disabled" in text:
            raise RuntimeError(
                "守护处于关闭状态，这次没有执行同步。"
                "请在「升级守护」里打开总开关，或直接点「立即同步引导」")
        if "拒绝同步" in text:
            raise RuntimeError(
                "同步被安全校验拦下（内核不完整或模块没装齐），引导分区没有改动。"
                "上方 WARN 一行写明了具体原因")
        if "RESULT=skip" in text:
            raise RuntimeError("本次没有执行同步：引导分区被升级程序占用或已是最新，详见上方日志")
        # 老版本脚本没有 RESULT 标记，沿用原判定
    job.log("同步完成", progress=100)
    job.ok({"output": text[-4000:]})


# ---------------------------------------------------------------------------
# 动作 2：诊断 + 修复
# ---------------------------------------------------------------------------
def action_repair(job):
    job.log("检查启动链……", progress=10)
    info = sysinfo.collect()
    if not info["support"]["supported"]:
        raise RuntimeError("；".join(info["support"]["reasons"]))
    chain = info["chain"]
    if chain["root_on_emmc"] and info["guard"]["lock"] == "ssd":
        job.log("当前从 eMMC 启动，尝试用 SSD 启动分区修复……", progress=30)
    else:
        job.log("当前从 %s 启动，执行常规同步" % chain["root_dev"], progress=30)
    rc, _ = run_guard(["repair"], job)
    if rc != 0:
        job.log("repair 返回非零，继续执行常规同步", "warn")
    rc, text = run_guard(["sync"], job)
    if rc != 0:
        raise RuntimeError("修复失败，请查看日志")
    info2 = sysinfo.collect(force=True)
    job.ok({"issues": info2["issues"], "output": text[-3000:]})


def action_dpkgfix(job):
    """把 dpkg 的配置从「装什么都在 4 秒内失败」里救出来。

    1.1.13 及以前版本在 /etc/dpkg/dpkg.cfg.d 下写的钩子会让 dpkg 直接
    报 configuration error，任何装包动作都在解析阶段终止。这里撤掉它、
    重装为无引号形式，并当场再让 dpkg 读一遍确认。
    """
    job.log("检查 dpkg 配置……", progress=10)
    broken, line, bad_file = guard.dpkg_cfg_problem()
    if not broken:
        job.log("dpkg 配置正常，无需修复", progress=60)
        job.ok({"repaired": False, "detail": "dpkg 配置正常"})
        return
    job.log("发现损坏的配置：%s" % (line or "configuration error"), "warn", progress=25)
    if bad_file:
        job.log("文件：%s" % bad_file, progress=30)
    ok, note = guard.repair_dpkg_cfg()
    if not ok:
        raise RuntimeError(note)
    job.log(note, progress=85)
    rc, out, err = run(["dpkg", "--audit"], timeout=20)
    audit = ((out or "") + (err or "")).strip()
    if "configuration error" in audit:
        raise RuntimeError("修复后 dpkg 仍报配置错误：%s" % audit[:400])
    job.log("dpkg --audit 已无配置错误", progress=95)
    job.ok({"repaired": True, "detail": note, "audit": audit[:800]})


# ---------------------------------------------------------------------------
# 动作 2b：升级前一键修复（把"不能升级"的项一次处理掉）
# ---------------------------------------------------------------------------
def action_upgrade_fix(job):
    job.log("检查升级前置条件……", progress=5)
    info = sysinfo.collect()
    up = info.get("upgrade") or {}
    fixes = [c.get("fix") for c in up.get("checks", []) if not c["ok"] and c.get("fix")]
    need_install = ("install" in fixes) or not info["guard"].get("installed")
    need_sync = ("sync" in fixes) or need_install

    # dpkg 坏了要先修，否则下面重装守护时写钩子也白写
    if "dpkgfix" in fixes:
        action_dpkgfix(job)

    if need_install:
        job.log("重新部署升级守护（关机同步单元 + apt/dpkg 钩子 + 升级监视）……", progress=20)
        install_guard(job)
    if need_sync:
        job.log("同步引导分区……", progress=55)
        rc, _ = run_guard(["sync", "--reason", "fix"], job)
        if rc != 0:
            raise RuntimeError("同步失败，请查看上方日志")

    info2 = sysinfo.collect(force=True)
    up2 = info2.get("upgrade") or {}
    bad = [c["label"] for c in up2.get("checks", []) if not c["ok"]]
    if not up2.get("ready"):
        raise RuntimeError("仍有未通过项：%s" % "、".join(bad))
    if bad:
        job.log("以下项目未通过但不影响升级：%s" % "、".join(bad), "warn")
    job.log("复检结论：%s" % up2.get("headline", "-"), progress=100)
    job.ok({"upgrade": up2})


# ---------------------------------------------------------------------------
# 动作 3：切换启动盘
# ---------------------------------------------------------------------------
def find_target_system(info, target):
    """返回 (root_part_dict, boot_part_path)。target: ssd|emmc"""
    want_emmc = (target == "emmc")
    cur = info["chain"]["root_dev"]
    for disk in info["disks"]:
        if disk["emmc"] != want_emmc:
            continue
        if disk["is_system"]:
            continue
        for part in disk["partitions"]:
            if part["role"] == "fnos-root":
                return part, _boot_part_of(disk)
    # 兜底：任何 fnOS 根分区
    for disk in info["disks"]:
        for part in disk["partitions"]:
            if part["role"] == "fnos-root" and part["path"] != cur:
                return part, _boot_part_of(disk)
    return None, None


def _boot_part_of(disk):
    """磁盘的启动分区：优先 role=boot/loader，其次第一个 vfat 分区。"""
    for p in disk["partitions"]:
        if p["role"] in ("boot", "loader"):
            return p["path"]
    for p in disk["partitions"]:
        if p["fstype"] == "vfat":
            return p["path"]
    for p in disk["partitions"]:
        if p.get("partuuid", "").endswith("-01"):
            return p["path"]
    return None


def _switch_to_emmc(job, boot, base):
    """把引导改回 eMMC：用 fnEnv.orig（首次同步前的原始配置）作为模板。"""
    with Mount(boot, readonly=False, prefix="oeswitch") as mnt:
        orig = os.path.join(mnt, ".oesboot", "fnEnv.orig")
        cur = os.path.join(mnt, "fnEnv.txt")
        template = read_file(orig) if os.path.isfile(orig) else read_file(cur)
        if not template:
            job.log("未找到可用的引导配置模板", "error")
            return 1
        names = bootenv.filenames(template)
        kf = names["kernelfile"]
        if kf and not os.path.isfile(os.path.join(mnt, kf)):
            job.log("eMMC 上已没有原始内核 %s（可能被空间清理），拒绝切换以免无法启动" % kf, "error")
            return 1
        job.log("使用%s作为模板：kernel=%s" % ("原始备份" if os.path.isfile(orig) else "当前配置", kf or "?"), progress=40)
        text = bootenv.render(template, base, base, kf, names["initrdfile"], names["fdtfile"])
        write_file(cur, text)
        job.log("已写入 %s（rootdev=%s-02）" % (cur, base), progress=80)
    return 0


def action_switch(job, target):
    if target not in ("ssd", "emmc"):
        raise ValueError("目标只能是 ssd 或 emmc")
    info = sysinfo.collect(force=True)
    if not info["support"]["supported"]:
        raise RuntimeError("；".join(info["support"]["reasons"]))

    part, boot = find_target_system(info, target)
    if not part:
        raise RuntimeError("没有找到%s上的 fnOS 系统分区" % ("SSD" if target == "ssd" else "eMMC"))
    job.log("目标系统：%s（%s，版本 %s）" % (part["path"], part["size_text"], part["version"] or "未知"), progress=15)

    if not boot:
        raise RuntimeError("目标磁盘缺少启动分区 p1")

    base = part["base"] or base_of(part["partuuid"])
    if not base:
        raise RuntimeError("目标分区没有 PARTUUID")

    if target == "emmc":
        # 切回 eMMC：优先使用首次同步前备份的原始引导配置，
        # 保证「旧内核 + 旧用户态」配套，而不是拿新内核去启动旧系统。
        rc = _switch_to_emmc(job, boot, base)
        if rc != 0:
            raise RuntimeError("写入引导配置失败")
    else:
        with Mount(boot, readonly=True, prefix="oeswitch") as mnt:
            env_text = read_file(os.path.join(mnt, "fnEnv.txt"))
            if not env_text:
                raise RuntimeError("%s 上没有 fnEnv.txt，无法切换" % boot)
            names = bootenv.filenames(env_text)
            if names["kernelfile"] and not os.path.exists(os.path.join(mnt, names["kernelfile"])):
                raise RuntimeError("%s 上缺少内核 %s，请先用固件修复该系统" % (boot, names["kernelfile"]))
            job.log("读取目标引导配置：kernel=%s" % (names["kernelfile"] or "?"), progress=30)
            rc, _ = run_guard(["sync", "--source", mnt, "--root-base", base, "--boot-base", base], job)
            if rc != 0:
                raise RuntimeError("写入引导配置失败")

    conf = guard.read_conf()
    conf["LOCK"] = target
    conf["TARGET_BASE"] = base
    conf["ENABLED"] = "1"
    guard.write_conf(conf, both=True)
    job.log("已锁定启动盘：%s（%s-02）" % (target.upper(), base), progress=90)

    info2 = sysinfo.collect(force=True)
    job.ok({
        "target": target,
        "target_base": base,
        "issues": info2["issues"],
        "need_reboot": True,
    })


# ---------------------------------------------------------------------------
# 动作 4：系统迁移（克隆到目标盘）
# ---------------------------------------------------------------------------
EXCLUDES = [
    "/proc", "/sys", "/dev", "/mnt", "/tmp", "/boot", "/media", "/run",
    "/lost+found", "/swapfile", "/swap.img", "/var/lib/oesboot",
]


def _rsync_args(src, dst):
    args = ["rsync", "-aHAX", "--numeric-ids", "--one-file-system", "--info=progress2"]
    for e in EXCLUDES:
        args.append("--exclude=" + e.lstrip("/"))
    # /vol* 是存储空间挂载点，全部排除
    args += ["--exclude=/vol*", "--exclude=/vol", "--exclude=/fs/*"]
    args += [src + "/", dst + "/"]
    return args


def disk_free_bytes(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def used_bytes():
    st = os.statvfs("/")
    return (st.f_blocks - st.f_bfree) * st.f_frsize


def action_migrate(job, disk_path, size_gb=None):
    info = sysinfo.collect(force=True)
    if not info["support"]["supported"]:
        raise RuntimeError("；".join(info["support"]["reasons"]))

    target = None
    for d in info["disks"]:
        if d["path"] == disk_path:
            target = d
            break
    if not target:
        raise RuntimeError("找不到磁盘 %s" % disk_path)
    if target["is_system"]:
        raise RuntimeError("不能把系统迁移到当前正在运行的系统盘")
    if target["emmc"]:
        raise RuntimeError("目标不能是 eMMC")
    for p in target["partitions"]:
        if p["mount"]:
            raise RuntimeError("%s 正在被挂载（%s），请先在存储管理中移除" % (p["path"], p["mount"]))

    need = used_bytes() * 1.1 + (256 << 20)
    if target["size"] < need:
        raise RuntimeError("目标盘容量不足：需要约 %s，实际 %s"
                           % (human_size(need), target["size_text"]))

    for tool in ("parted", "rsync", "mkfs.vfat", "mkfs.btrfs"):
        if not shutil.which(tool):
            raise RuntimeError("缺少命令 %s，无法执行迁移（请先在系统上安装）" % tool)

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    ptable_backup = os.path.join(BACKUP_DIR, "%s-%s.sfdisk" % (os.path.basename(disk_path), stamp))
    rc, out, _ = run(["sfdisk", "-d", disk_path])
    if rc == 0:
        write_file(ptable_backup, out)
        job.log("已备份分区表到 %s" % ptable_backup, progress=5)

    # ---- 分区 ----
    src_boot = info["chain"]["boot_part"]
    boot_size = 1 << 30
    if src_boot and os.path.exists(src_boot):
        rc, out, _ = run(["blockdev", "--getsize64", src_boot])
        if rc == 0:
            boot_size = max(int(out.strip()), 512 << 20)
    boot_size = min(boot_size, 2 << 30)

    root_size = None
    if size_gb:
        root_size = int(float(size_gb) * (1 << 30))

    job.log("创建分区表（boot %s + root %s）……"
            % (human_size(boot_size), human_size(root_size) if root_size else "剩余全部"), progress=12)
    run(["umount", "-f", disk_path + "1"], timeout=30)
    run(["wipefs", "-a", "-f", disk_path], timeout=60)

    parted = shutil.which("parted")
    if not parted:
        raise RuntimeError("缺少 parted 命令，无法分区（fnOS 应自带，请检查）")

    cmds = [
        [parted, "-s", disk_path, "mklabel", "gpt"],
        [parted, "-s", "-a", "optimal", disk_path, "mkpart", "fat32", "1MiB", str(boot_size + (1 << 20)) + "B"],
    ]
    if root_size:
        start = boot_size + (1 << 20)
        cmds.append([parted, "-s", "-a", "optimal", disk_path, "mkpart", "btrfs",
                     str(start) + "B", str(start + root_size) + "B"])
    else:
        cmds.append([parted, "-s", "-a", "optimal", disk_path, "mkpart", "btrfs",
                     str(boot_size + (1 << 20)) + "B", "100%"])
    for c in cmds:
        rc, out, err = run(c, timeout=120)
        if rc != 0:
            raise RuntimeError("分区失败：%s" % (err or out).strip()[-300:])

    run(["udevadm", "settle"], timeout=30)
    p1, p2 = disk_path + "1", disk_path + "2"
    for _ in range(10):
        if os.path.exists(p1) and os.path.exists(p2):
            break
        time.sleep(0.5)
    if not (os.path.exists(p1) and os.path.exists(p2)):
        raise RuntimeError("分区节点未就绪：%s / %s" % (p1, p2))

    # ---- 格式化 ----
    job.log("格式化 %s（FAT32）与 %s（btrfs）……" % (p1, p2), progress=20)
    rc, out, err = run(["mkfs.vfat", "-F", "32", "-n", "OESBOOT", p1], timeout=180)
    if rc != 0:
        raise RuntimeError("格式化启动分区失败：%s" % (err or out)[-300:])
    rc, out, err = run(["mkfs.btrfs", "-f", "-L", "OESROOT", p2], timeout=300)
    if rc != 0:
        raise RuntimeError("格式化根分区失败：%s" % (err or out)[-300:])

    # ---- 克隆 ----
    job.log("挂载目标根分区……", progress=28)
    with Mount(p2, readonly=False, prefix="oesmig") as dst:
        run(["btrfs", "property", "set", dst, "compression", "zstd:3"])
        job.log("开始克隆系统文件（耗时较长，请勿断电）……", progress=35)
        args = _rsync_args("/", dst)
        proc_rc, text = _run_stream(args, job, base_progress=35, span=45)
        if proc_rc != 0:
            raise RuntimeError("克隆失败（rsync 退出码 %s），目标盘未启用" % proc_rc)

        job.log("重建挂载点目录……", progress=82)
        for d in ("proc", "sys", "dev", "mnt", "tmp", "boot", "media", "run", "lost+found"):
            os.makedirs(os.path.join(dst, d), exist_ok=True)

        # ---- fstab ----
        job.log("改写目标系统 fstab……", progress=84)
        pu1 = blkid_value(p1, "PARTUUID")
        pu2 = blkid_value(p2, "PARTUUID")
        if not pu1 or not pu2:
            raise RuntimeError("无法读取新分区的 PARTUUID")
        _rewrite_fstab(os.path.join(dst, "etc/fstab"), pu1, pu2)

        # ---- 校验 ----
        job.log("校验克隆结果……", progress=88)
        checks = []
        for rel, name in (("usr/trim", "fnOS 组件目录"), ("etc/fstab", "fstab"),
                          ("bin/sh", "shell"), ("etc/systemd/system", "systemd 单元")):
            checks.append((rel, os.path.exists(os.path.join(dst, rel)), name))
        bad = [n for _, okk, n in checks if not okk]
        if bad:
            raise RuntimeError("克隆结果不完整，缺少：%s" % "、".join(bad))
        job.log("文件校验通过", progress=90)

    # ---- 启动分区 ----
    job.log("写入目标启动分区……", progress=92)
    with Mount(p1, readonly=False, prefix="oesmigboot") as bdst:
        src_boot_dir = info["chain"]["boot_dir"] or "/boot"
        _copy_boot(src_boot_dir, bdst)
        env_src = os.path.join(bdst, "fnEnv.txt")
        if not os.path.exists(env_src):
            raise RuntimeError("目标启动分区缺少 fnEnv.txt")
        text = read_file(env_src)
        base = base_of(pu2)
        names = bootenv.filenames(text)
        new_env = bootenv.render(text, base, base, names["kernelfile"],
                                 names["initrdfile"], names["fdtfile"])
        write_file(env_src, new_env)
        job.log("目标启动分区配置完成（rootdev=%s-02）" % base, progress=95)

    # ---- 同步到实际引导分区 ----
    job.log("同步引导分区（u-boot 读取的那一份）……", progress=96)
    with Mount(p1, readonly=True, prefix="oesmigsync") as msrc:
        rc, _ = run_guard(["sync", "--source", msrc, "--root-base", base, "--boot-base", base], job)
    if rc != 0:
        raise RuntimeError("写入引导分区失败，系统仍从原盘启动（可重试）")

    conf = guard.read_conf()
    conf["ENABLED"] = "1"
    conf["LOCK"] = "ssd"
    conf["TARGET_BASE"] = base
    guard.write_conf(conf, both=True)

    state = _load_state()
    state["last_migration"] = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "disk": disk_path, "boot": p1, "root": p2,
        "base": base, "ptable_backup": ptable_backup,
    }
    _save_state(state)

    job.log("迁移完成。重启后将从 %s 启动；若失败，eMMC 上的守护会自动回切。" % disk_path, "warn", 100)
    job.ok({
        "disk": disk_path, "base": base, "boot": p1, "root": p2,
        "need_reboot": True, "backup": ptable_backup,
    })


def _run_stream(args, job, base_progress=0, span=50):
    """流式执行命令，把百分比推进到 job.progress。"""
    import subprocess
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True, bufsize=1)
    except FileNotFoundError:
        return 127, "rsync 未安装"
    last = -1
    for line in proc.stdout:
        line = line.rstrip("\n")
        m = re.search(r"(\d+)%", line)
        if m:
            pct = int(m.group(1))
            if pct != last and pct % 5 == 0:
                job.progress = base_progress + int(pct * span / 100.0)
                last = pct
        if line.strip() and not m:
            job.log(line[:300])
    proc.wait()
    return proc.returncode, ""


def _copy_boot(src, dst):
    """把 /boot 的内容复制到目标启动分区（FAT 不支持权限，忽略属性错误）。"""
    if shutil.which("rsync"):
        run(["rsync", "-r", "--no-perms", "--no-owner", "--no-group",
             "--modify-window=1", "--delete", src + "/", dst + "/"], timeout=600)
        return
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s):
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
            shutil.copytree(s, d)
        else:
            shutil.copyfile(s, d)


def _rewrite_fstab(path, boot_pu, root_pu):
    text = read_file(path)
    if not text:
        raise RuntimeError("目标系统缺少 /etc/fstab")
    out = []
    seen_root = seen_boot = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        parts = re.split(r"\s+", stripped)
        if len(parts) < 2:
            out.append(line)
            continue
        mount = parts[1]
        if mount == "/":
            parts[0] = "PARTUUID=" + root_pu
            seen_root = True
            out.append("\t".join(parts))
        elif mount == "/boot":
            parts[0] = "PARTUUID=" + boot_pu
            seen_boot = True
            out.append("\t".join(parts))
        else:
            out.append(line)
    if not seen_root:
        out.append("PARTUUID=%s\t/\tbtrfs\tdefaults,noatime,compress=zstd:3\t0 0" % root_pu)
    if not seen_boot:
        out.append("PARTUUID=%s\t/boot\tvfat\tdefaults,umask=0022\t0 2" % boot_pu)
    write_file(path, "\n".join(out) + "\n")


def _load_state():
    try:
        return json.loads(read_file(os.path.join(STATE_DIR, "state.json")) or "{}")
    except ValueError:
        return {}


def _save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    write_file(os.path.join(STATE_DIR, "state.json"), json.dumps(state, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 动作 5：配置
# ---------------------------------------------------------------------------
def set_policy(lock=None, auto_repair=None, auto_reboot=None, enabled=None, target_base=None):
    conf = guard.read_conf()
    if lock is not None:
        if lock not in ("follow", "ssd", "emmc"):
            raise ValueError("lock 只能是 follow/ssd/emmc")
        conf["LOCK"] = lock
        if lock == "follow":
            conf["TARGET_BASE"] = ""
    if auto_repair is not None:
        conf["AUTO_REPAIR"] = "1" if auto_repair else "0"
    if auto_reboot is not None:
        conf["AUTO_REBOOT"] = "1" if auto_reboot else "0"
    if enabled is not None:
        conf["ENABLED"] = "1" if enabled else "0"
    if target_base is not None:
        conf["TARGET_BASE"] = target_base
    guard.write_conf(conf, both=True)
    return conf


def reboot(delay=0):
    if delay:
        run(["sh", "-c", "sleep %d; systemctl reboot" % delay], timeout=5)
        return {"ok": True, "delay": delay}
    run(["systemctl", "reboot"], timeout=20)
    return {"ok": True}


def install_guard(job=None):
    script = os.path.join(APP_DIR, "assets", "oes-boot-sync.sh")
    if not os.path.isfile(script):
        raise RuntimeError("应用缺少 assets/oes-boot-sync.sh")
    conf = guard.read_conf()
    conf["ENABLED"] = conf.get("ENABLED", "1")
    report = guard.install(script, conf)
    if job:
        for r in report:
            job.log("%s：%s %s" % (r["target"], "已部署" if r["ok"] else "跳过", r["note"]))
    return report
