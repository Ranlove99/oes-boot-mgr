"""fnEnv.txt（OES u-boot 环境文件）解析与重写。

格式示例（每行 key=value）：

    verbosity=1
    bootlogo=false
    console=both
    fdtfile=amlogic/meson-g12b-s922x-oes-plus.dtb
    kernelfile=vmlinuz-6.18.18.c944-trim
    rootdev=PARTUUID=0060e0e9-02
    instdisk_bootuuid=0060e0e9-01
    instdisk_rootuuid=0060e0e9-02
    initrdfile=uInitrd-6.18.18.c944-trim

注意：fnOS 某些版本会给 instdisk_* 的值加一个前导双引号（如
`instdisk_bootuuid="0060e0e9-01`），这是机器的既有格式，重写时必须保留，
否则 u-boot 解析不到对应分区。因此这里只替换值里的 PARTUUID 基址，
其余字符原样保留。
"""
import re

UUID_BASE_RE = re.compile(r"[0-9a-fA-F]{8}-")
KEY_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$")


def parse(text):
    """返回 (dict, lines)。dict: key -> value（保留引号等原始字符）。"""
    data = {}
    lines = text.splitlines()
    for line in lines:
        m = KEY_RE.match(line)
        if m:
            data[m.group(1)] = m.group(2)
    return data, lines


def get(text, key, default=""):
    data, _ = parse(text)
    return data.get(key, default)


def _rebase(value, new_base, part="-02"):
    """把值里的 PARTUUID 基址（8 位十六进制 + 连字符）替换为新基址。

    值里没有可替换的 UUID（例如写成了 /dev/sda2）时，整体改写成 PARTUUID 形式。
    """
    if not new_base:
        return value
    new = UUID_BASE_RE.sub(new_base + "-", value, count=1)
    if new_base not in new:
        prefix = "PARTUUID=" if value.strip().startswith("PARTUUID=") or not value.strip().startswith("/") else ""
        return "%s%s%s" % (prefix, new_base, part)
    return new


def render(template, root_base, boot_base, kernelfile="", initrd="", fdt=""):
    """基于模板重写 fnEnv.txt 内容。

    root_base  : 目标根分区 PARTUUID 基址
    boot_base  : 目标系统盘启动分区 PARTUUID 基址（instdisk_bootuuid 用）
    """
    if not root_base:
        raise ValueError("root_base 不能为空")
    boot_base = boot_base or root_base

    out = []
    seen = set()
    for line in template.splitlines():
        m = KEY_RE.match(line)
        if not m:
            out.append(line)
            continue
        key = m.group(1)
        if key == "kernelfile" and kernelfile:
            out.append("kernelfile=%s" % kernelfile)
            seen.add(key)
        elif key == "initrdfile" and initrd:
            out.append("initrdfile=%s" % initrd)
            seen.add(key)
        elif key == "fdtfile" and fdt:
            out.append("fdtfile=%s" % fdt)
            seen.add(key)
        elif key == "rootdev":
            out.append("rootdev=%s" % _rebase(m.group(2), root_base))
            seen.add(key)
        elif key == "instdisk_bootuuid":
            out.append("instdisk_bootuuid=%s" % _rebase(m.group(2), boot_base, "-01"))
            seen.add(key)
        elif key == "instdisk_rootuuid":
            out.append("instdisk_rootuuid=%s" % _rebase(m.group(2), root_base))
            seen.add(key)
        else:
            out.append(line)

    if "kernelfile" not in seen and kernelfile:
        out.append("kernelfile=%s" % kernelfile)
    if "initrdfile" not in seen and initrd:
        out.append("initrdfile=%s" % initrd)
    if "fdtfile" not in seen and fdt:
        out.append("fdtfile=%s" % fdt)
    if "rootdev" not in seen:
        out.append("rootdev=PARTUUID=%s-02" % root_base)
    if "instdisk_bootuuid" not in seen:
        out.append("instdisk_bootuuid=%s-01" % boot_base)
    if "instdisk_rootuuid" not in seen:
        out.append("instdisk_rootuuid=%s-02" % root_base)

    return "\n".join(out) + "\n"


def filenames(text):
    """取 fnEnv 中引用的内核/initrd/dtb 文件名。"""
    data, _ = parse(text)
    def clean(v):
        return v.strip().strip('"').strip("'").strip()
    return {
        "kernelfile": clean(data.get("kernelfile", "")),
        "initrdfile": clean(data.get("initrdfile", "")),
        "fdtfile": clean(data.get("fdtfile", "")),
        "rootdev": clean(data.get("rootdev", "")),
    }


def env_root_base(text):
    """从 rootdev 值里取基址。"""
    v = filenames(text)["rootdev"]
    m = UUID_BASE_RE.search(v)
    return m.group(0)[:-1] if m else ""
