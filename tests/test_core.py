#!/usr/bin/env python3
"""核心逻辑单元测试（不接触真实磁盘）：python3 tests/test_core.py"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from core import bootenv, sysinfo  # noqa: E402
from core import ops  # noqa: E402

SAMPLE = """verbosity=1
bootlogo=false
console=both
fdtfile=amlogic/meson-g12b-s922x-oes-plus.dtb
kernelfile=vmlinuz-6.18.18.c944-trim
rootdev=PARTUUID=0060e0e9-02
instdisk_bootuuid="0060e0e9-01
instdisk_rootuuid="PARTUUID=0060e0e9-02
initrdfile=uInitrd-6.18.18.c944-trim
"""


class BootEnvTest(unittest.TestCase):
    def test_render_rebases_quoted_values(self):
        out = bootenv.render(SAMPLE, "abcd1234", "abcd1234",
                             "vmlinuz-new", "uInitrd-new", "amlogic/x.dtb")
        self.assertIn("rootdev=PARTUUID=abcd1234-02", out)
        self.assertIn('instdisk_bootuuid="abcd1234-01', out)   # 保留前导引号
        self.assertIn('instdisk_rootuuid="PARTUUID=abcd1234-02', out)
        self.assertIn("kernelfile=vmlinuz-new", out)
        self.assertIn("initrdfile=uInitrd-new", out)
        self.assertIn("fdtfile=amlogic/x.dtb", out)
        self.assertIn("console=both", out)  # 未涉及的键原样保留

    def test_render_appends_missing_keys(self):
        out = bootenv.render("verbosity=1\n", "deadbeef", "deadbeef", "k1", "i1", "")
        self.assertIn("rootdev=PARTUUID=deadbeef-02", out)
        self.assertIn("instdisk_bootuuid=deadbeef-01", out)
        self.assertIn("kernelfile=k1", out)
        self.assertNotIn("fdtfile=", out)

    def test_render_keeps_partuuid_prefix_style(self):
        src = "rootdev=0060e0e9-02\ninstdisk_rootuuid=0060e0e9-02\n"
        out = bootenv.render(src, "11112222", "11112222")
        self.assertIn("rootdev=11112222-02", out)  # 不带 PARTUUID= 时也保持一致

    def test_filenames_strips_quotes(self):
        names = bootenv.filenames(SAMPLE)
        self.assertEqual(names["kernelfile"], "vmlinuz-6.18.18.c944-trim")
        self.assertEqual(names["initrdfile"], "uInitrd-6.18.18.c944-trim")

    def test_env_root_base(self):
        self.assertEqual(bootenv.env_root_base(SAMPLE), "0060e0e9")


def make_info(loader_kernel="vmlinuz-old", boot_kernel="vmlinuz-new", lock="ssd",
              root_on_emmc=False, installed=True, enabled=True,
              boot_part="/dev/mmcblk1p1"):
    info = {
        "chain": {
            "root_dev": "/dev/sda2", "root_partuuid": "aabbccdd-02", "root_base": "aabbccdd",
            "root_disk": "sda", "root_on_emmc": root_on_emmc, "boot_dir": "/boot",
            "boot_part": boot_part, "boot_base": "aabbccdd", "ubootpart": "/dev/mmcblk1p1",
            "loader_part": "/dev/mmcblk1p1", "loader_base": "11223344", "loader_avail_kb": 200000,
            "cmdline": "root=PARTUUID=aabbccdd-02 ubootpart=/dev/mmcblk1p1",
            "boot_env": "kernelfile=%s\n" % boot_kernel,
            "boot_env_files": {"kernelfile": boot_kernel, "initrdfile": "", "fdtfile": "",
                               "rootdev": "PARTUUID=aabbccdd-02"},
            "loader_env": "kernelfile=%s\n" % loader_kernel,
            "loader_env_kernel": loader_kernel, "loader_env_root": "PARTUUID=aabbccdd-02",
            "loader_files": [],
        },
        "guard": {"installed": installed, "enabled": enabled, "lock": lock,
                  "auto_repair": True, "auto_reboot": False, "last_sync": {}, "log_tail": []},
        "disks": [],
        "support": {},
    }
    info["support"] = sysinfo.support_of(info)
    info["issues"] = sysinfo.analyze(info)
    return info


class AnalyzeTest(unittest.TestCase):
    def codes(self, info):
        return [i["code"] for i in info["issues"]]

    def test_healthy(self):
        self.assertIn("healthy", self.codes(make_info("k", "k")))

    def test_kernel_desync(self):
        self.assertIn("kernel-desync", self.codes(make_info()))

    def test_guard_missing(self):
        self.assertIn("no-guard", self.codes(make_info("k", "k", installed=False)))

    def test_emmc_fallback(self):
        self.assertIn("emmc-fallback", self.codes(make_info("k", "k", root_on_emmc=True)))

    def test_unsupported(self):
        info = make_info("k", "k")
        info["chain"]["loader_part"] = ""
        info["chain"]["boot_env"] = ""
        info["chain"]["root_base"] = ""
        info["support"] = sysinfo.support_of(info)
        info["issues"] = sysinfo.analyze(info)
        self.assertIn("unsupported", self.codes(info))


class FstabTest(unittest.TestCase):
    def test_rewrite(self):
        src = ("# comment\n"
               "/dev/mmcblk1p2\t/\tbtrfs\tdefaults\t0 0\n"
               "/dev/mmcblk1p1\t/boot\tvfat\tdefaults\t0 2\n"
               "/dev/sdb1\t/vol1\tbtrfs\tdefaults\t0 0\n")
        with tempfile.NamedTemporaryFile("w", suffix=".fstab", delete=False) as fh:
            fh.write(src)
            path = fh.name
        try:
            ops._rewrite_fstab(path, "newboot-01", "newroot-02")
            with open(path) as fh:
                text = fh.read()
            self.assertIn("PARTUUID=newroot-02\t/\tbtrfs", text)
            self.assertIn("PARTUUID=newboot-01\t/boot\tvfat", text)
            self.assertIn("/vol1", text)
            self.assertIn("# comment", text)
        finally:
            os.unlink(path)


class TargetPickTest(unittest.TestCase):
    def test_boot_part_detection(self):
        disk = {"partitions": [
            {"path": "/dev/sda1", "role": "boot", "fstype": "vfat", "partuuid": "x-01"},
            {"path": "/dev/sda2", "role": "fnos-root", "fstype": "btrfs", "partuuid": "x-02"},
        ]}
        self.assertEqual(ops._boot_part_of(disk), "/dev/sda1")

    def test_boot_part_fallback_vfat(self):
        disk = {"partitions": [
            {"path": "/dev/nvme0n1p1", "role": "unknown", "fstype": "vfat", "partuuid": "y-01"},
        ]}
        self.assertEqual(ops._boot_part_of(disk), "/dev/nvme0n1p1")


class UpgradeReadyTest(unittest.TestCase):
    """升级就绪判定：只有"内核/rootdev 对不上"才该拦下升级。"""

    def up(self, loader_kernel="k", loader_root="PARTUUID=aabbccdd-02",
           loader_fdt="", boot_fdt="", avail=200000):
        info = make_info(loader_kernel, "k")
        info["chain"]["loader_env_root"] = loader_root
        info["chain"]["loader_avail_kb"] = avail
        info["chain"]["boot_env_files"]["fdtfile"] = boot_fdt
        return sysinfo.upgrade_status(info, {"fdtfile": loader_fdt}, {})

    def test_same_kernel_is_ready(self):
        up = self.up()
        self.assertTrue(up["ready"])
        # 测试环境没有 systemd 单元，preboot 会是 warn；它是兜底项，不拦升级
        self.assertIn(up["verdict"], ("safe", "warn"))
        self.assertTrue(all(c["ok"] for c in up["checks"] if c["level"] == "error"))

    def test_kernel_desync_blocks(self):
        info = make_info("vmlinuz-old", "vmlinuz-new")
        up = sysinfo.upgrade_status(info, {}, {})
        self.assertFalse(up["ready"])
        self.assertEqual(up["verdict"], "block")
        bad = [c["key"] for c in up["checks"] if not c["ok"]]
        self.assertIn("kernel", bad)

    def test_rootdev_mismatch_blocks(self):
        up = self.up(loader_root="PARTUUID=ffffffff-02")
        self.assertFalse(up["ready"])
        self.assertIn("rootdev", [c["key"] for c in up["checks"] if not c["ok"]])

    def test_fdt_and_space_are_soft(self):
        up = self.up(loader_fdt="a.dtb", boot_fdt="b.dtb", avail=50 * 1024)
        self.assertTrue(up["ready"])          # 软项不拦升级
        self.assertEqual(up["verdict"], "warn")
        self.assertIn("fdt", [c["key"] for c in up["checks"] if not c["ok"]])

    def test_layout_mismatch_warns_but_not_block(self):
        """u-boot 读 eMMC、/boot 却挂 SSD：这是隐患，但不该挡住启动守护。

        1.1.15 之前它是 error 级 → verdict=block → 前端"启动升级守护"被拒，
        而"一键修复"又不处理布局，用户就卡在死循环里。布局不会让装包当场失败
        （真正会让装包失败的是 dpkg 配置坏掉），所以降为 warn。
        """
        info = make_info("k", "k", boot_part="/dev/sda1")
        up = sysinfo.upgrade_status(info, {}, {})
        self.assertTrue(up["ready"])                      # 可以启动守护
        self.assertEqual(up["verdict"], "warn")
        self.assertIn("layout", [c["key"] for c in up["checks"] if not c["ok"]])
        self.assertIn("引导布局", up["headline"])
        lay = [c for c in up["checks"] if c["key"] == "layout"][0]
        self.assertEqual(lay["level"], "warn")
        self.assertEqual(lay["fix"], "hybrid")            # 仍给出一键修复入口

    def test_layout_ok_when_boot_on_loader(self):
        info = make_info("k", "k", boot_part="/dev/mmcblk1p1")
        up = sysinfo.upgrade_status(info, {}, {})
        self.assertTrue([c for c in up["checks"] if c["key"] == "layout"][0]["ok"])

    def test_last_shutdown_matched(self):
        info = make_info("k", "k")
        up = sysinfo.upgrade_status(info, {}, {"time": "2026-11-07 00:40:00",
                                               "kernelfile": "k", "reason": "shutdown"})
        self.assertTrue(up["last_shutdown"]["synced"])
        self.assertTrue(up["last_shutdown"]["matches"])

    def test_dpkg_cfg_broken_blocks_everything(self):
        """dpkg 读不了配置文件时，任何装包都会秒失败——必须压过其他所有项。"""
        from core import guard
        orig = guard.dpkg_cfg_problem
        guard.dpkg_cfg_problem = lambda: (
            True,
            "dpkg: error: configuration error: /etc/dpkg/dpkg.cfg.d/99oesboot-sync:1: "
            "unbalanced quotes in 'post-invoke'",
            "/etc/dpkg/dpkg.cfg.d/99oesboot-sync",
        )
        try:
            info = make_info("k", "k", boot_part="/dev/sda1")  # 布局也不对
            up = sysinfo.upgrade_status(info, {}, {})
            self.assertEqual(up["verdict"], "block")
            self.assertFalse(up["ready"])
            self.assertIn("dpkg", up["headline"])          # dpkg 优先于布局
            bad = [c["key"] for c in up["checks"] if not c["ok"]]
            self.assertIn("dpkgcfg", bad)
        finally:
            guard.dpkg_cfg_problem = orig

    def test_dpkg_hook_is_quote_free(self):
        """post-invoke 一旦带引号就可能被 dpkg 判成不配平，整机包管理瘫痪。"""
        from core import guard
        self.assertNotIn('"', guard.DPKG_HOOK)
        self.assertNotIn("'", guard.DPKG_HOOK)
        self.assertNotIn(">", guard.DPKG_HOOK)
        self.assertNotIn("|", guard.DPKG_HOOK)
        self.assertIn("post-invoke /usr/local/sbin/oesboot-dpkg-hook", guard.DPKG_HOOK)


if __name__ == "__main__":
    unittest.main(verbosity=2)
