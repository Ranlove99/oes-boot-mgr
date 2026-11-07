"""通用工具：命令执行、挂载、文件、格式化。仅依赖标准库。"""
import json
import os
import re
import shlex
import subprocess
import tempfile
import time

RUN_TIMEOUT = 120


class CommandError(RuntimeError):
    def __init__(self, cmd, rc, out, err):
        self.cmd = cmd
        self.rc = rc
        self.out = out
        self.err = err
        super().__init__("命令失败(%s): %s\n%s" % (rc, " ".join(cmd), (err or out)[-800:]))


def run(cmd, check=False, timeout=RUN_TIMEOUT, input_data=None):
    """执行命令，返回 (rc, stdout, stderr)。不抛异常，除非 check=True。"""
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            universal_newlines=True,
        )
        out, err = proc.communicate(input_data, timeout=timeout)
        rc = proc.returncode
    except FileNotFoundError:
        out, err, rc = "", "命令不存在: %s" % cmd[0], 127
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err, rc = "", "命令超时: %s" % " ".join(cmd), 124
    if check and rc != 0:
        raise CommandError(cmd, rc, out, err)
    return rc, out, err


def run_out(cmd, default="", timeout=RUN_TIMEOUT):
    rc, out, _ = run(cmd, timeout=timeout)
    return out.strip() if rc == 0 else default


def which(name):
    for p in os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin").split(":"):
        f = os.path.join(p, name)
        if os.path.isfile(f) and os.access(f, os.X_OK):
            return f
    return None


def human_size(num):
    try:
        num = float(num)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0 or unit == "TB":
            if unit == "B":
                return "%d B" % num
            return "%.1f %s" % (num, unit)
        num /= 1024.0
    return "-"


def parse_size(text):
    """把 32G / 512M 解析成字节。"""
    if text is None:
        return None
    m = re.match(r"^\s*([0-9.]+)\s*([kKmMgGtT]?)", str(text))
    if not m:
        return None
    val = float(m.group(1))
    mult = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}[m.group(2).lower()]
    return int(val * mult)


def read_file(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return default


def write_file(path, content, mode=0o644):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def append_file(path, content):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(content)


def is_rootfs_writable():
    return os.access("/", os.W_OK)


def blkid_value(dev, tag):
    return run_out(["blkid", "-s", tag, "-o", "value", dev])


def base_of(partuuid):
    """PARTUUID 的磁盘基址：0060e0e9-02 -> 0060e0e9"""
    if not partuuid:
        return ""
    m = re.match(r"^([0-9a-fA-F]{8})-", partuuid)
    return m.group(1) if m else ""


def dev_exists(dev):
    return bool(dev) and os.path.exists(dev)


class Mount(object):
    """临时挂载上下文管理器；若设备已挂载则复用其挂载点（不卸载别人挂的）。"""

    def __init__(self, dev, readonly=True, fstype=None, prefix="oesboot"):
        self.dev = dev
        self.readonly = readonly
        self.fstype = fstype
        self.prefix = prefix
        self.dir = None
        self._owned = False

    def __enter__(self):
        existing = run_out(["findmnt", "-n", "-o", "TARGET", self.dev])
        if existing:
            self.dir = existing.splitlines()[0].strip()
            return self.dir
        base = tempfile.mkdtemp(prefix="%s-" % self.prefix, dir="/run")
        opts = ["ro"] if self.readonly else ["rw"]
        cmd = ["mount"]
        if self.fstype:
            cmd += ["-t", self.fstype]
        cmd += ["-o", ",".join(opts), self.dev, base]
        rc, out, err = run(cmd)
        if rc != 0 and self.fstype:
            rc, out, err = run(["mount", "-o", ",".join(opts), self.dev, base])
        if rc != 0:
            os.rmdir(base)
            raise RuntimeError("挂载 %s 失败：%s" % (self.dev, (err or out).strip()[-300:]))
        self.dir = base
        self._owned = True
        return self.dir

    def __exit__(self, *exc):
        if self._owned and self.dir:
            run(["umount", self.dir])
            try:
                os.rmdir(self.dir)
            except OSError:
                pass
        return False


def wait_for(predicate, timeout=10.0, interval=0.2):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


def load_json(text, default=None):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default
