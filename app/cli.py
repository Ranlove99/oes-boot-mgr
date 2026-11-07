#!/usr/bin/env python3
"""命令行入口：oesboot <command>

  oesboot status                 查看启动链状态
  oesboot upgrade                升级就绪判定（能不能直接点升级）
  oesboot watch start|stop       开启 / 结束升级监视
  oesboot diagnose               输出完整诊断报告
  oesboot sync                   立即同步引导分区
  oesboot repair                 检查并修复
  oesboot switch ssd|emmc        切换启动盘（改引导，重启生效）
  oesboot migrate /dev/sdX [GB]  迁移系统到目标盘
  oesboot guard install|status   部署 / 查看守护
  oesboot policy lock=ssd auto_reboot=1
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import guard, ops, sysinfo  # noqa: E402


def cmd_status():
    info = sysinfo.collect()
    c, g = info["chain"], info["guard"]
    print("系统版本 : %s  (内核 %s)" % (info["host"]["version"] or "?", info["host"]["kernel"]))
    print("根分区   : %s  %s" % (c["root_dev"], c["root_partuuid"]))
    print("启动分区 : %s   (u-boot 实际读取: %s)" % (c["boot_part"] or "-", c["loader_part"] or "-"))
    print("系统内核 : %s" % c["boot_env_files"]["kernelfile"])
    print("引导内核 : %s" % (c["loader_env_kernel"] or "-"))
    print("引导root : %s" % (c["loader_env_root"] or "-"))
    print("守护     : %s  锁定=%s 自动修复=%s 自动重启=%s" % (
        "已启用" if g["enabled"] else "未启用", g["lock"],
        "on" if g["auto_repair"] else "off", "on" if g["auto_reboot"] else "off"))
    print("上次同步 : %s" % (g["last_sync"].get("time") or "从未"))
    up = info.get("upgrade") or {}
    if up:
        print("升级结论 : %s（%s）" % (up.get("verdict"), up.get("headline")))
        w = up.get("watch") or {}
        if w.get("active"):
            print("升级监视 : 运行中，已自动同步 %d 次" % w.get("hits", 0))
        else:
            print("升级监视 : 未开启（oesboot watch start）")
    print("")
    for issue in info["issues"]:
        print("[%s] %s" % (issue["level"].upper(), issue["title"]))
        if issue["detail"]:
            print("      %s" % issue["detail"])


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 0
    cmd = argv[1]

    if cmd in ("status", "st"):
        cmd_status()
        return 0
    if cmd == "diagnose":
        print(sysinfo.diagnose_text(sysinfo.collect()))
        return 0
    if cmd == "upgrade":
        info = sysinfo.collect()
        up = info.get("upgrade") or {}
        print("结论: %s（%s）" % (up.get("verdict"), up.get("headline")))
        print("")
        for c in up.get("checks", []):
            print("  [%s] %-18s %s" % ("OK" if c["ok"] else c["level"].upper(), c["label"], c["detail"]))
        w = up.get("watch") or {}
        print("")
        print("监视: %s%s" % ("运行中" if w.get("active") else "未开启",
                            ("，自 %s 起已同步 %d 次" % (w.get("since", ""), w.get("hits", 0))) if w.get("active") else ""))
        return 0
    if cmd == "watch":
        sub = argv[2] if len(argv) > 2 else "status"
        if sub == "start":
            print(guard.enable_watch())
        elif sub == "stop":
            print(guard.disable_watch())
        else:
            print(guard.watch_status())
        return 0
    if cmd == "sync":
        rc, out = ops.run_guard(["sync"])
        print(out)
        return rc
    if cmd == "repair":
        rc, out = ops.run_guard(["repair"])
        print(out)
        rc2, out2 = ops.run_guard(["sync"])
        print(out2)
        return rc or rc2
    if cmd == "switch":
        target = (argv[2] if len(argv) > 2 else "").lower()
        if target not in ("ssd", "emmc"):
            print("用法: oesboot switch ssd|emmc")
            return 1
        job = ops.start_job("switch", "switch", lambda j: ops.action_switch(j, target))
        return _wait(job)
    if cmd == "migrate":
        if len(argv) < 3:
            print("用法: oesboot migrate /dev/sdX [根分区GB]")
            return 1
        size = float(argv[3]) if len(argv) > 3 else None
        job = ops.start_job("migrate", "migrate", lambda j: ops.action_migrate(j, argv[2], size))
        return _wait(job)
    if cmd == "guard":
        sub = argv[2] if len(argv) > 2 else "status"
        if sub == "install":
            print(json.dumps(ops.install_guard(), ensure_ascii=False, indent=2))
            return 0
        print(json.dumps(guard.read_conf(), ensure_ascii=False, indent=2))
        return 0
    if cmd == "policy":
        kwargs = {}
        for arg in argv[2:]:
            if "=" not in arg:
                continue
            k, v = arg.split("=", 1)
            if k in ("lock",):
                kwargs["lock"] = v
            elif k in ("auto_repair", "auto_reboot", "enabled"):
                kwargs[k] = v in ("1", "true", "on", "yes")
        print(json.dumps(ops.set_policy(**kwargs), ensure_ascii=False, indent=2))
        return 0
    print("未知命令: %s" % cmd)
    return 1


def _wait(job, timeout=1800):
    import time
    start = time.time()
    while job.status == "running" and time.time() - start < timeout:
        time.sleep(1)
        for step in job.steps[len(getattr(_wait, "_seen", [])):]:
            print("[%s] %s" % (step["ts"], step["msg"]))
        _wait._seen = list(job.steps)
    if job.status == "success":
        print("完成")
        return 0
    print("失败: %s" % job.result.get("error", ""))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
